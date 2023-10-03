"""Implement SDR classes using SoapySDR. Allows to use a single or multiple
SDRs in parallel using threads."""

import time
import numpy as np
from threading import Thread
import SoapySDR

import lib.log as l
import lib.load as load

class MySoapySDRs():
    def __init__(self):
        l.LOGGER.debug("MySoapySDRs.__init__()")
        self.sdrs = []

    def register(self, sdr):
        l.LOGGER.debug("MySoapySDRs.register(idx={})".format(sdr.idx))
        self.sdrs.append(sdr)
        # Temporary hack to be compatible with nrf52_whad.py who access
        # self.radio.fs variable. This imply to have the same sampling rate
        # accross two SDRs.
        self.fs = sdr.fs

    def open(self):
        l.LOGGER.debug("MySoapySDRs.open()")
        for sdr in self.sdrs:
            sdr.open()

    def close(self):
        l.LOGGER.debug("MySoapySDRs.close()")
        for sdr in self.sdrs:
            sdr.close()

    def record(self, N):
        """Perform a recording of N samples.

        Spawn a thread for each radio and start recording. Block until all
        recordings finished and all threads join.

        """
        l.LOGGER.debug("MySoapySDRs.record(N={}).enter".format(N))
        thr = [None] * len(self.sdrs)
        for sdr in self.sdrs:
            thr[sdr.idx] = Thread(target=sdr.record, args=(N,))
            thr[sdr.idx].start()
        for sdr in self.sdrs:
            thr[sdr.idx].join()
        l.LOGGER.debug("MySoapySDRs.record(N={}).exit".format(N))

    def accept(self):
        l.LOGGER.debug("MySoapySDRs.accept()")
        for sdr in self.sdrs:
            sdr.accept()

    def save(self, dir):
        l.LOGGER.debug("MySoapySDRs.save(dir={})".format(dir))
        for sdr in self.sdrs:
            sdr.save(dir)

class MySoapySDR():
    # * Custom dtype
    # It is used to match the CS16 type of SoapySDR, allowing to save disk
    # space but requires conversion happening in this module, since Numpy can
    # only work with np.complex64 using float32.
    DTYPE = np.dtype([('real', np.int16), ('imag', np.int16)])

    @staticmethod
    def numpy_save(file, arr):
        """Stub for numpy.save handling our custom dtype.

        Save to disk the trace stored in ARR using our custom dtype. ARR.dtype
        can be np.complex64 (will be converted) or MySoapySDR.DTYPE (will be
        saved as it).

        """
        assert(arr.dtype == np.complex64 or arr.dtype == MySoapySDR.DTYPE)
        if arr.dtype == np.complex64:
            arr = MySoapySDR.complex64_to_dtype(arr)
        arr.tofile(file)

    @staticmethod
    def numpy_load(file):
        """Stub for numpy.load handling our custom dtype.

        The loaded FILE has to be in the MySoapySDR.DTYPE format, which will be
        converted into np.complex64 for processing.

        """
        return MySoapySDR.dtype_to_complex64(np.fromfile(file, dtype=MySoapySDR.DTYPE))

    @staticmethod
    def dtype_to_complex64(arr):
        """Convert an array from our custom DTYPE to a standard np.complex64
        (composed of 2 np.float32)."""
        assert(arr.dtype == MySoapySDR.DTYPE)
        # Don't need to check any boundaries here since casting from np.int16
        # to np.float32 is safe.
        return arr.view(np.int16).astype(np.float32).view(np.complex64)

    @staticmethod
    def complex64_to_dtype(arr):
        """Convert an array from a standard np.complex64 (composed of 2
        np.float32) to our custom DTYPE."""
        assert(arr.dtype == np.complex64)
        # Check that no value contained in arr is superior to maximum or
        # inferior to minimum of np.int16 (-2^15 or +2^15), since casting from
        # np.float32 to np.int16 is not safe.
        assert(arr[arr.real < np.iinfo(np.int16).min].shape == (0,))
        assert(arr[arr.real > np.iinfo(np.int16).max].shape == (0,))
        assert(arr[arr.imag < np.iinfo(np.int16).min].shape == (0,))
        assert(arr[arr.imag > np.iinfo(np.int16).max].shape == (0,))
        return arr.view(np.float32).astype(np.int16).view(MySoapySDR.DTYPE)

    def __init__(self, fs, freq, idx = 0, enabled = True):
        l.LOGGER.debug("MySoapySDR.__init__(fs={},freq={},idx={})".format(fs, freq, idx))
        self.fs = fs
        self.freq = freq
        self.idx = idx
        self.enabled = enabled
        if self.enabled:
            results = SoapySDR.Device.enumerate()
            self.sdr = SoapySDR.Device(results[idx])
            self.sdr.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, fs)
            self.sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, freq)
            self.sdr.setGain(SoapySDR.SOAPY_SDR_RX, 0, 76)
            self.sdr.setAntenna(SoapySDR.SOAPY_SDR_RX, 0, "TX/RX")

    def open(self):
        if self.enabled:
            l.LOGGER.debug("MySoapySDR(idx={}).open()".format(self.idx))
            # From SoapySDR/include/SoapySDR/Device.h:
            # - "CF32" - complex float32 (8 bytes per element)
            # - "CS16" - complex int16   (4 bytes per element)
            # From SoapyUHD/SoapyUHDDevice.cpp/getNativeStreamFormat():
            # UHD and the hardware use "CS16" format in the underlying transport layer.
            self.rx_stream = self.sdr.setupStream(SoapySDR.SOAPY_SDR_RX, SoapySDR.SOAPY_SDR_CS16)
            self.sdr.activateStream(self.rx_stream)
            self.rx_signal = np.array([0], MySoapySDR.DTYPE)

    def close(self):
        if self.enabled:
            l.LOGGER.debug("MySoapySDR(idx={}).close().enter".format(self.idx))
            self.sdr.deactivateStream(self.rx_stream)
            self.sdr.closeStream(self.rx_stream)
            l.LOGGER.debug("MySoapySDR(idx={}).close().leave".format(self.idx))

    def record(self, N):
        if self.enabled:
            l.LOGGER.debug("MySoapySDR(idx={}).record(N={:e}).enter".format(self.idx, N))
            N = int(N) # Required when N is specified using scientific notation.
            rx_buff_len = pow(2, 24)
            rx_buff = np.array([0] * rx_buff_len, MySoapySDR.DTYPE)
            self.rx_signal_candidate = np.array([0], MySoapySDR.DTYPE)
            while len(self.rx_signal_candidate) < N:
                sr = self.sdr.readStream(self.rx_stream, [rx_buff], rx_buff_len, timeoutUs=10000000)
                if sr.ret == rx_buff_len and sr.flags == 1 << 2:
                    self.rx_signal_candidate = np.concatenate((self.rx_signal_candidate, rx_buff))
            l.LOGGER.debug("MySoapySDR(idx={}).record().leave".format(self.idx))
        else:
            time.sleep(N/1e7)

    def accept(self):
        if self.enabled:
            l.LOGGER.debug("MySoapySDR(idx={}).accept()".format(self.idx))
            self.rx_signal = np.concatenate((self.rx_signal, self.rx_signal_candidate))

    def save(self, dir):
        if self.enabled:
            l.LOGGER.debug("MySoapySDR(idx={}).save(dir={})".format(self.idx, dir))
            load.save_raw_trace(self.rx_signal, dir, self.idx, 0)
