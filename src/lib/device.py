# Core modules.
import enum
import dataclasses
import re
from time import sleep, time
import random
import os
import struct
import serial
import secrets

# Local modules.
import lib.dataset as dataset
from lib.dataset import InputSource, InputGeneration
import lib.utils as utils
import lib.device as device
import lib.log as l

# External modules.
import numpy as np
try:
    from scapy.all import BTLE_DATA, BTLE_ADV, ATT_Hdr, L2CAP_Hdr, ATT_Read_Request, ATT_Read_Multiple_Request, ATT_Find_Information_Request, BTLE_EMPTY_PDU, BTLE_CTRL, LL_ENC_REQ, LL_ENC_RSP, LL_START_ENC_REQ, LL_REJECT_IND
    import whad
    from whad.ble import Central, ConnectionEventTrigger, ReceptionTrigger
    from whad.ble.profile import UUID
    from whad.ble.bdaddr import BDAddress
    from whad.ble.stack.llm import START_ENC_REQ, REJECT_IND
    from whad.ble.stack.smp import CryptographicDatabase, Pairing, IOCAP_KEYBD_DISPLAY
    from whad.device import WhadDevice
except ImportError as e: # Don't make these modules mandatory for running all the app.
    l.LOGGER.error("Can't import WHAD! Error: {}".format(e))

# * Classes

class Device():
    # Timeout limit used for the loops of this module [s].
    TIMEOUT = 20

    # WHAD's central instantiated from an HCI dongle.
    hci = None
    # Set to true if self.hci needs to be used.
    hci_is_needed = False
    # WHAD's security database used during pairing.
    secdb = None
    # WHAD's security material get after pairing.
    secentry = None
    # DeviceInput object handling input generation and source methods.
    input = None
    # The received LL_ENC_RSP packet sent by the target device. Must be saved
    # by a callback.
    enc_rsp = None
    # Count of LL_REJECT_IND received packets.
    reject_ind_cnt = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __alert_ll_reject_ind(self, pkt):
        """Callback alerting if more than a single LL_REJECT_IND is received."""
        if pkt.haslayer(LL_REJECT_IND):
            self.reject_ind_cnt += 1
            if self.reject_ind_cnt > 1:
                l.LOGGER.error("LL_REJECT_IND received!")

    def __save_ll_enc_rsp(self, pkt):
        """Callback filtering packets and saving every LL_ENC_RSP packets for
        further processing. Will throw a DEBUG message for a saved packet.

        """
        if pkt.haslayer(LL_ENC_RSP):
            # print(repr(pkt.metadata))
            # pkt.show()
            l.LOGGER.debug("Save the received LL_ENC_RSP packet!")
            self.enc_rsp = pkt

    def __init__(self, cfg, ser_port, baud, bd_addr_src, bd_addr_dest, radio, dset, sset):
        self.cfg = DeviceConfig(cfg)
        self.ser_port = ser_port
        self.baud = baud
        self.bd_addr_src = bd_addr_src
        self.bd_addr_dest = bd_addr_dest
        self.radio = radio
        self.dataset = dset
        self.subset = sset
        # Do we need to use an HCI dongle?
        if self.subset.input_src == dataset.InputSource.PAIRING:
            self.hci_is_needed = True
        self.secdb = CryptographicDatabase()
        try:
            l.LOGGER.info("Instantiate Central using dongle on UART0")
            self.central = Central(WhadDevice.create('uart0'))
            if self.hci_is_needed is True:
                l.LOGGER.info("Instantiate Central using dongle on HCI0")
                self.hci = Central(WhadDevice.create('hci0'), security_database=self.secdb)
        except Exception as e:
             # NOTE: Use __repr__ because WHAD exceptions doesn't have
             # descriptions, only names accessible through __repr__().
            raise Exception("{}".format(e.__repr__()))
        l.LOGGER.info("Spoof bluetooth address: {}".format(self.bd_addr_src))
        self.central.set_bd_address(self.bd_addr_src)
        # If we receive a LL_ENC_RSP packet, save it to parse the SKD later.
        self.central.attach_callback(self.__save_ll_enc_rsp)
        self.central.attach_callback(self.__alert_ll_reject_ind)
        self.time_start = time()
        self.time_elapsed = 0
        self.input = DeviceInput(self, dset, sset, ser_port, baud)

    def __timeouted(self, raise_exc=False):
        """Return True if timeout is exceeded with RAISE_EXC set to False, or
        raise an Exception with RAISE_EXC set to True.

        """
        self.time_elapsed = time() - self.time_start
        timeouted = self.time_elapsed >= Device.TIMEOUT
        if timeouted is True and raise_exc is True:
            raise Exception("timeout of {}s is exceeded!".format(Device.TIMEOUT))
        else:
            return timeouted

    def __pair__(self):
        """Establish a pairing with the target device.

        This functions is meant to be used when generating inputs using a
        pairing.

        """
        assert self.hci is not None, "HCI dongle has not been instantiated!"
        l.LOGGER.info("Pair with target device...")
        l.LOGGER.debug("Connect...")
        # NOTE: random=True is important otherwise no connection.
        conn = self.hci.connect(self.bd_addr_dest, random=True)
        # NOTE: Pairing parameters replicating the ones used by Mirage.
        l.LOGGER.debug("Pair...")
        conn.pairing(pairing=Pairing(
            lesc=False,
            mitm=False,
            bonding=True,
            iocap=IOCAP_KEYBD_DISPLAY,
            sign_key=False,
            id_key=False,
            link_key=False,
            enc_key=True
        ))
        l.LOGGER.debug("Pairing successfull!")
        # Get the relevant cryptographic material from crypto database.
        # NOTE: We have to precise the random=True otherwise we will not get
        # correct entry.
        self.secentry = self.secdb.get(address=BDAddress(self.bd_addr_dest, random=True))
        l.LOGGER.debug(self.secentry)
        l.LOGGER.debug("Disconnect...")
        conn.disconnect()
        l.LOGGER.info("Pairing done!")

    def configure(self, idx):
        l.LOGGER.info("Configure device for recording index #{}".format(idx))
        # Get the input, from the dataset, from the serial port, or from pairing.
        self.input.get(idx)
        # Put the input to the serial port if needed.
        self.input.put(idx)
        # NOTE: IVM can be kept set to 0 since it will only be used after the
        # session key derivation (hence, after our recording and our
        # instrumentation).
        self.ivm = 0x00000000

    def execute(self):
        l.LOGGER.debug("Start preparing WHAD's sequences...")
        # At specified connection event, send an empty packet, used to
        # inform the radio to start recording at a precise connection
        # event.
        l.LOGGER.info("Connection event for starting the recording: {}".format(self.cfg.start_radio_conn_event))
        trgr_start_radio = ConnectionEventTrigger(self.cfg.start_radio_conn_event)
        self.central.prepare(
            BTLE_DATA() / BTLE_EMPTY_PDU(),
            trigger=trgr_start_radio
        )

        # At specified connection event, send the ATT_Read_Requests and the
        # LL_ENC_REQ. (MD=1) force the ATT_Read_Response to be on the same
        # connection event as the ENC_RSP, excepting to have the
        # ATT_Read_Response during AES processing. If you set the MD bit
        # before, the connection events will be separated.
        l.LOGGER.info("Connection event for sending the LL_ENC_REQ request: {}".format(self.cfg.ll_enc_req_conn_event))
        trgr_send_ll_enc_req = ConnectionEventTrigger(self.cfg.ll_enc_req_conn_event)
        # l.LOGGER.info("Procedure interleaving: {}".format(self.cfg.procedure_interleaving))
        # l.LOGGER.info("More data bit: MD={}".format(self.cfg.more_data_bit))
        if self.cfg.procedure_interleaving is True:
            l.LOGGER.info("Procedure interleaving method: {}".format(self.cfg.procedure_interleaving_method.name))
            self.central.prepare(
                BTLE_DATA()     / L2CAP_Hdr() / ATT_Hdr() / self.cfg.procedure_interleaving_method,
                BTLE_DATA(MD=self.cfg.more_data_bit) / BTLE_CTRL() / LL_ENC_REQ(rand=self.input.rand, ediv=self.input.ediv, skdm=self.input.skdm, ivm=self.ivm),
                trigger=trgr_send_ll_enc_req
            )
        else:
            self.central.prepare(
                BTLE_DATA(MD=self.cfg.more_data_bit) / BTLE_CTRL() / LL_ENC_REQ(rand=self.input.rand, ediv=self.input.ediv, skdm=self.input.skdm, ivm=self.ivm),
                trigger=trgr_send_ll_enc_req
            )
        l.LOGGER.debug("central.prepare(LL_ENC_REQ[rand=0x{:x}, ediv=0x{:x}, skdm=0x{:x}, ivm=0x{:x}])".format(self.input.rand, self.input.ediv, self.input.skdm, self.ivm))

        # When receiveing a LL_START_ENC_REQ packet, send an empty packet,
        # used to count the number of successful link encryption to know
        # how many trace we should have captured.
        trgr_recv_ll_start_enc_req = ReceptionTrigger(
            packet=BTLE_DATA() / BTLE_CTRL() / LL_START_ENC_REQ(),
            selected_fields=("opcode")
        )
        self.central.prepare(
            BTLE_DATA() / BTLE_EMPTY_PDU(),
            trigger=trgr_recv_ll_start_enc_req
        )

        # If receiveing a LL_REJECT_IND packet, send an empty packet. The
        # goal here is just to know that we have to raise an error, meaning
        # that EDIV/RAND/BD_ADDR aren't correct and that legitimate
        # connection sniffing needs to be redone.
        trgr_recv_ll_reject_ind = ReceptionTrigger(
            packet=BTLE_DATA() / BTLE_CTRL() / LL_REJECT_IND(),
            selected_fields=("opcode")
        )
        self.central.prepare(
            BTLE_DATA() / BTLE_EMPTY_PDU(),
            trigger=trgr_recv_ll_reject_ind
        )

        # Connect to the peripheral. The parameters are:
        # 1. Use increased hop interval. Decreasing it speed-up the connection.
        # 2. Set channel map to 0x300 which corresponds to channel 8-9.
        l.LOGGER.info("Connect to target device...")
        l.LOGGER.debug("Connection parameters: address={}, random=False, hop_interval={}, channel_map=0x{:x}, timeout={}".format(self.bd_addr_dest, self.cfg.hop_interval, self.cfg.channel_map, self.TIMEOUT))
        device = self.central.connect(self.bd_addr_dest, random=False, hop_interval=self.cfg.hop_interval, channel_map=self.cfg.channel_map, timeout=self.TIMEOUT)

        if self.central.is_connected():
            l.LOGGER.debug("WHAD's central is connected to target device!")
            # Wait until the connection event we should start the radio.
            while not self.__timeouted(raise_exc=True) and not trgr_start_radio.triggered:
                pass
            # The radio has been started too late if LL_START_ENC_REQ is
            # already received.
            if trgr_recv_ll_start_enc_req.triggered:
                raise Exception("The recording hasn't been started while we received the encryption confirmation!")
            # Start the recording and wait for it to complete.
            self.radio.record()
            # The recording isn't likely to contain the AES since we didn't
            # received an LL_START_ENC_REQ. The recording is maybe happening
            # too soon.
            if not trgr_recv_ll_start_enc_req.triggered:
                raise Exception("The recording finished while we didn't received the encryption confirmation!")
            else:
                self.radio.accept()

            l.LOGGER.info("Disconnect from the target device")
            device.disconnect()
        else:
            raise Exception("Cannot connect to target device!")
        if trgr_recv_ll_reject_ind.triggered:
            raise Exception("LL_REJECT_IND packet received, LL_ENC_REQ request's parameters were not accepted!")

    def save(self, idx):
        """Save necessary data from the device.

        This functions if meant to be called after a successfull
        instrumentation for recording index IDX.

        If the input generation method is pairing at runtime, this function
        will get the used input for the AES (LTK for key, SKD for plaintext)
        and save it inside our dataset. Save also the security material
        generated during the pairing for later usage.

        """
        if self.subset.input_gen == dataset.InputGeneration.RUN_TIME and self.subset.input_src == dataset.InputSource.PAIRING:
            # Get the SKDS and concatenate with SKDM.
            skds = self.enc_rsp.lastlayer().fields["skds"]
            l.LOGGER.debug("Received SKDS=0x{:x}".format(skds))
            skd = skds << 64 | self.input.skdm
            # Save the used key (LTK) and used plaintext (SKD) to our dataset.
            self.subset.set_current_ks(idx, utils.bytes_hex_to_npy_int2(self.secentry.ltk.value, 16))
            self.subset.set_current_pt(idx, utils.str_hex_to_npy_int(utils.int_to_str_hex(skd, 16)))
            # Save the security entry in the dataset object such that DeviceInput
            # can reload it if needed.
            self.subset.saved_secentry = self.secentry

    def close(self):
        if self.central is not None:
            l.LOGGER.debug("Stop and close UART0 dongle...")
            self.central.stop()
            self.central.close()
            self.central = None
            if self.hci_is_needed is True:
                l.LOGGER.debug("Stop and close HCI dongle...")
                self.hci.stop()
                self.hci.close()
                self.hci = None

class DeviceConfig:
    """Configuration for the Device class."""

    # Connection event number when to start the radio.
    start_radio_conn_event = None
    # Connection event number for when sending the LL_ENC_REQ packet.
    ll_enc_req_conn_event = None
    # Hop interval.
    hop_interval = None
    # Channel map.
    channel_map = None
    # More Data Bit.
    more_data_bit = None
    # Procedure interleaving flag.
    procedure_interleaving = None
    # Procedure interleaving request (Scapy).
    procedure_interleaving_method = None

    def __init__(self, cfg):
        """Initialize a DeviceConfig.

        :param cfg: Dictionnary representing the TOML configuration.

        """
        self.start_radio_conn_event = cfg["start_radio_conn_event"]
        self.ll_enc_req_conn_event = cfg["ll_enc_req_conn_event"]
        self.hop_interval = cfg["hop_interval"]
        self.channel_map = cfg["channel_map"]
        self.more_data_bit = cfg["more_data_bit"]
        self.procedure_interleaving = cfg["procedure_interleaving"]
        if self.procedure_interleaving is True:
            if cfg["procedure_interleaving_method"] == "att_read_request":
                self.procedure_interleaving_method = ATT_Read_Request(gatt_handle=3)
            elif cfg["procedure_interleaving_method"] == "att_read_multiple_request_2":
                self.procedure_interleaving_method = ATT_Read_Multiple_Request(handles=[3, 3])
            elif cfg["procedure_interleaving_method"] == "att_read_multiple_request_3":
                self.procedure_interleaving_method = ATT_Read_Multiple_Request(handles=[3, 3, 3])
            elif cfg["procedure_interleaving_method"] == "att_read_multiple_request_4":
                self.procedure_interleaving_method = ATT_Read_Multiple_Request(handles=[3, 3, 3, 3])
            elif cfg["procedure_interleaving_method"] == "att_find_information_request":
                self.procedure_interleaving_method = ATT_Find_Information_Request()
                
class DeviceInput():
    """Handle the different cases of generating and storing input.

    - If the input has been generated at dataset initialization time, it will
      send the input from the dataset to the serial port.

    - If the input has to be generated during runtime, it will either:

      1. Use the serial port to get a new input, store it in the dataset, and
      resend it on the serial port to configure the device.

      2. Use a pairing to get a new input, store it in the dataset, and do not
      open a serial connection.

    """

    # Device object that instantiated the DeviceInput.
    dev = None
    # Dataset.
    dset = None
    # Subset.
    sset = None
    # Serial port for getting and putting input.
    ser_port = None
    # Baudrate for serial port.
    baud = None
    # RAND used in the connection.
    rand = None
    # EDIV used in the connection.
    ediv = None
    # SKDM used in the connection.
    skdm = None

    def __init__(self, dev, dset, sset, ser_port, baud):
        """Initialize the DeviceInput. It will later use the dataset's
        parameters for input generation and input source. It also must know
        about serial port and WHAD centrals to use them if needed to get/put
        the inputs.

        """
        assert type(dev) == Device
        assert type(dset) == dataset.Dataset
        assert type(sset) == dataset.Subset
        self.dev = dev
        self.dset = dset
        self.sset = sset
        self.ser_port = ser_port
        self.baud = baud

    @staticmethod
    def write_to_ser(ser, cmd):
        """Write the command CMD to the serial port SER for our custom
        firmware.

        """
        # NOTE: Needs to convert the string to bytes using .encode().
        # NOTE: Needs "\n\n" at the end to actually sends the command.
        l.LOGGER.debug("ser <- {}".format(cmd))
        ser.write("{}\n\n".format(cmd).encode())
        sleep(0.1)

    def configure_dataset_runtime(self, idx):
        """If needed, configure the dataset input at run time.

        Get the input generated at run time by get it from the device.

        IDX is the current recording index.

        """
        def configure_get_input():
            """Get input from the serial port.

            Use the serial port to get random numbers used as plaintext and key for
            the current run.

            Return a tuple composed of (KEY, PLAINTEXT) being both two 2D np.array
            using "dtype=np.uint8".

            """

            def read_input_from_ser(ser):
                """Read an input from the serial port SER.

                An input is a hexadecimal number of 16 bytes sent as 32 hex digits
                in ASCII. It is readed as a "bytes" Python class.

                """
                # NOTE: Get rid of 5 first "k?\r\r\n0x" and last 5 "\r\n\r\r\n".
                readed = ser.read(44)[7:-5]
                l.LOGGER.debug("ser -> {}".format(readed))
                # Discard next bytes to prepare for next read.
                discard = ser.read_until()
                return readed

            def get_input_from_ser(ser, input_type):
                """Get the input from serial port.

                Send a command on the serial port SER allowing to get an input
                based on INPUT_TYPE ["k" or "p"]. Read this input and return it as
                "bytes".

                """
                assert(input_type == "k" or input_type == "p")
                DeviceInput.write_to_ser(ser, "{}?".format(input_type))
                readed = read_input_from_ser(ser)
                l.LOGGER.info("Got {}={}".format(input_type, readed))
                return readed

            l.LOGGER.info("Get p and k from serial port...")
            with serial.Serial(self.ser_port, self.baud) as ser:
                ks = utils.bytes_hex_to_npy_int(get_input_from_ser(ser, "k"))
                pt = utils.bytes_hex_to_npy_int(get_input_from_ser(ser, "p"))
            return ks, pt

        # Get random numbers from serial port.
        ks, pt = configure_get_input()
        # Save those random numbers as plaintext and keys in our dataset.
        self.sset.set_current_ks(idx, ks)
        self.sset.set_current_pt(idx, pt)

    def configure_ser(self, k, p):
        """Configure the input of our custom firmware using serial port.

        """
        def sub_input_to_ser(ser):
            """Submit input to the Nimble security database of our custom firmware."""
            DeviceInput.write_to_ser(ser, "input_sub")

        def write_input_to_ser(ser, input, input_type):
            """Write an input (a key or a plaintext) INPUT represented by a string
            containing an hexidecimal number to the serial port. INPUT_TYPE can be
            set to 'p' or 'k'.

            """
            assert(type(input) == str)
            assert(input_type == "k" or input_type == "p")
            l.LOGGER.info("Send {}={}".format(input_type, input))
            DeviceInput.write_to_ser(ser, "{}:{}".format(input_type, input))

        l.LOGGER.info("Send p and k on serial port...")
        with serial.Serial(self.ser_port, self.baud) as ser:
            # Convert dataset to input for firmware over serial port and send it.
            write_input_to_ser(ser, utils.npy_int_to_str_hex(k), "k")
            write_input_to_ser(ser, utils.npy_int_to_str_hex(p), "p")
            sub_input_to_ser(ser)
            DeviceInput.write_to_ser(ser, "input_dump") # NOTE: Keep it here because otherwise sub_input is not sent properly.

    def get(self, idx):
        """Get a new input into the dataset based on configured methods for
        recording index IDX.

        It will configure the RAND, the EDIV and the SKDM to use in the
        connection parameters.

        """
        def set_fixed_input():
            """This function configure our DeviceInput with a fixed and
            hardcoded input composed of RAND, EDIV and SKDM.

            """
            # NOTE: RAND and EDIV values are hardcoded twice, here and in our
            # custom firmware inside input.c.
            self.rand = 0xdeadbeefdeadbeef
            self.ediv = 0xdead
            # NOTE: SKDM can be kept set to 0 since we will submit a plaintext
            # for our custom firmware.
            self.skdm = 0x00000000

        def set_cryptomat_input():
            """Configure our DeviceInput with a dynamic input coming from a
            CryptographicMaterial from WHAD. It will get the RAND and the EDIV
            from the cryptographic material and will generate the SKDM.

            """
            # Store the EDIV and RAND from security database.
            assert type(self.dev.secentry.ltk.rand) == bytes
            assert type(self.dev.secentry.ltk.ediv) == int
            self.rand = utils.bytes_hex_to_int_single(self.dev.secentry.ltk.rand)
            self.ediv = self.dev.secentry.ltk.ediv
            # Generate a SKDM.
            self.skdm = int(secrets.token_hex(8), base=16)
            l.LOGGER.debug("Generated SKDM=0x{:016x}".format(self.skdm))

        # * If the input is already generated, we don't need to get it.
        if self.sset.input_gen == dataset.InputGeneration.INIT_TIME:
            # Set fixed input in the connection since the real input will be send over the serial port.
            set_fixed_input()
        # * If the input has to be get from the serial port and is hardcoded in the firmware...
        elif self.sset.input_gen == dataset.InputGeneration.RUN_TIME and self.sset.input_src == dataset.InputSource.SERIAL:
            # Configure the subset input from the serial port.
            self.configure_dataset_runtime(idx)
            # Set fixed input in the connection since the real input will be send over the serial port.
            set_fixed_input()
        # * If the input has to be get from a pairing...
        elif self.sset.input_gen == dataset.InputGeneration.RUN_TIME and self.sset.input_src == dataset.InputSource.PAIRING:
            # Choose between new pairing or resuming inputs from last pairing.
            # NOTE: We always want to pair at index 0 or if keys are variable
            # (e.g. in train set).
            if idx == 0 or self.sset.ks_type == dataset.InputType.VARIABLE:
                # Pair with the target device to generate inputs.
                self.dev.__pair__()
            # Resume parameters from first pairing for a fixed key.
            elif idx > 0 and self.sset.ks_type == dataset.InputType.FIXED:
                l.LOGGER.info("Restore security material from last pairing")
                self.dev.secentry = self.sset.saved_secentry
            # Set the generated or resumed input inside our class.
            set_cryptomat_input()
        # Sanity-check for further execution.
        assert type(self.rand) == int and type(self.ediv) == int and type(self.skdm) == int
        assert self.rand != None and self.ediv != None and self.skdm != None

    def put(self, idx):
        """Put a new input into the device based on configured methods for
        recording index IDX.

        """
        # If we should send to the serial port the input generated at
        # initialization time or got from the serial port.
        if self.sset.input_gen == dataset.InputGeneration.INIT_TIME or (
                self.sset.input_gen == dataset.InputGeneration.RUN_TIME and self.sset.input_src == dataset.InputSource.SERIAL):
            # Send real input on serial port.
            self.configure_ser(k=self.sset.get_current_ks(idx), p=self.sset.get_current_pt(idx))
        # If we should do nothing since the input will be carry in the
        # connection parameters.
        elif self.sset.input_gen == dataset.InputGeneration.RUN_TIME and self.sset.input_src == dataset.InputSource.PAIRING:
            pass

    def __str__(self):
        string = "rand=0x{:x}".format(self.rand)
        string += "\nediv=0x{:x}".format(self.ediv)
        string += "\nskdm=0x{:x}".format(self.skdm)
        return string

# NOTE: Skeleton for a new instrumentation code.
# class DeviceCustom():
#     # Parameters of custom firware.
    
#     # Serial port [str].
#     ser_port = None
#     # Baud rate for serial connection [int].
#     baud = None

#     # Python objects.
    
#     # Initialized radio object [MySoapySDR].
#     radio = None
#     # Used dataset reference [Dataset].
#     dset = None
#     # Used subset reference [Subset].
#     sset = None
    
#     def __enter__(self):
#         return self

#     def __exit__(self, *args):
#         pass

#     def __init__(self, ser_port, baud, radio, dset, sset):
#         l.LOGGER.info("Initilize custom device...")
#         # Set objects variables.
#         self.ser_port = ser_port
#         self.baud = baud
#         self.radio = radio
#         self.dset = dset
#         self.sset = sset
#         # Sanity check.
#         assert sset.input_src == InputSource.SERIAL
#         assert sset.input_gen == InputGeneration.INIT_TIME

#     def configure(self, idx):
#         l.LOGGER.info("Configure custom device for index {}...".format(idx))
#         pass

#     def execute(self):
#         l.LOGGER.info("Execute custom device instrumentation...")
#         pass
