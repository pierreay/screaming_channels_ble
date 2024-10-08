"""Classes representing dataset."""

from multiprocessing import Process, Queue
import os
from os import path
from enum import Enum
import numpy as np
import matplotlib.pyplot as plt
import pickle
import signal
import sys
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

# Necessary because WHAD classes are dynamically stored in Subset object from device.py.
import whad

import lib.input_generators as input_generators
import lib.load as load
import lib.log as l
import lib.plot as libplot
import lib.complex as complex
import lib.analyze as analyze
import lib.utils as utils
import lib.debug as debug

# NOTE: start=0 because used to index tuples returning (traces_nf, traces_ff).
TraceType = Enum('TraceType', ['NF', 'FF'], start=0)
SubsetType = Enum('SubsetType', ['TRAIN', 'ATTACK'])
InputType = Enum('InputType', ['FIXED', 'VARIABLE'])
InputGeneration = Enum('InputGeneration', ['RUN_TIME', 'INIT_TIME'])
InputSource = Enum('InputSource', ['SERIAL', 'PAIRING'])

# Global reference to a DatasetProcessing object used for the signal handler.
DPROC = None

class Dataset():
    """Top-level class representing a dataset."""
    FILENAME = "dataset.pyc"

    def __init__(self, name, dir, samp_rate):
        self.name = name
        self.dir = dir
        self.dirsave = dir
        self.samp_rate = samp_rate
        self.train_set = None
        self.attack_set = None
        self.profile = None
        self.dirty = False
        self.dirty_idx = 0
        self.run_resumed = False

    def __str__(self):
        string = "dataset '{}':\n".format(self.name)
        string += "- dir: {}\n".format(self.dir)
        string += "- dirsave: {}\n".format(self.dirsave)
        string += "- samp_rate: {:.2e}\n".format(self.samp_rate)
        string += "- dirty: {}\n".format(self.dirty)
        string += "- dirty_idx: {}\n".format(self.dirty_idx)
        string += "- dirty_savedir: {}\n".format(self.get_savedir_dirty())
        string += "- run_resumed: {}\n".format(self.run_resumed)
        if self.train_set is not None:
            string += str(self.train_set)
        if self.attack_set is not None:
            string += str(self.attack_set)
        if self.profile is not None:
            string += str(self.profile)
        return string

    @staticmethod
    def get_path_static(dir):
        return path.join(dir, Dataset.FILENAME)

    @staticmethod
    def is_pickable(dir):
        return path.exists(Dataset.get_path_static(dir))

    @staticmethod
    def pickle_load(dir_path, log=True, quit_on_error=False):
        if not Dataset.is_pickable(dir_path):
            if quit_on_error is True:
                l.LOGGER.error("dataset doesn't exists!")
                exit(-1)
            else:
                return None
        with open(Dataset.get_path_static(dir_path), "rb") as f:
            pickled = pickle.load(f)
            assert(type(pickled) == Dataset)
            pickled.dir = dir_path     # Update Dataset.dir (self.dir) when pickling.
            pickled.dirsave = dir_path # Update Dataset.dirsave (self.dirsave) when pickling.
        if pickled.train_set is not None:
            pickled.train_set.load_input()
        if pickled.attack_set is not None:
            pickled.attack_set.load_input()
        pickled.run_resumed = False
        if log is True:
            l.LOGGER.info("Dataset loaded from '{}'".format(Dataset.get_path_static(dir_path)))
        return pickled

    def get_path(self, save=False):
        return Dataset.get_path_static(self.dir if save is False else self.dirsave)

    def set_dirsave(self, dirsave):
        """Set saving directory of current Dataset and create subdirectories. for
        registered Subset accordingly."""
        assert(path.exists(dirsave))
        self.dirsave = dirsave
        self.create_dirsave()

    def create_dirsave(self):
        """Create directories for registered Subset accordingly in the saving
        directory."""
        assert(path.exists(self.dirsave))
        if self.train_set is not None:
            os.makedirs(path.join(self.dirsave, self.train_set.dir), exist_ok=True)
        if self.attack_set is not None:
            os.makedirs(path.join(self.dirsave, self.attack_set.dir), exist_ok=True)

    def get_savedir_dirty(self):
        if path.exists(path.join(self.dirsave, Dataset.FILENAME)):
            dset = Dataset.pickle_load(self.dirsave, log=False)
            return dset.dirty
        return False

    def resume_from_savedir(self, subset=None):
        assert(Dataset.is_pickable(self.dirsave))
        dset_dirsave = Dataset.pickle_load(self.dirsave)
        self.run_resumed = True
        self.dirty = dset_dirsave.dirty
        self.dirty_idx = dset_dirsave.dirty_idx
        if subset is not None:
            sset = self.get_subset(subset)
            sset_dirsave = dset_dirsave.get_subset(subset)
            sset.template = sset_dirsave.template
            sset.bad_entries = sset_dirsave.bad_entries

    def pickle_dump(self, force=False, unload=True, log=True):
        """Dump the Dataset on the disk.

        This function will create the saving directory if needed to save the
        dataset. If FORCE is set to False [default], the function will ask for
        confirmation to overwrite the dataset in the same directory.

        The saving operation consist of dumping the input of all subsets. If
        UNLOAD is set to True [default], the inputs and the traces will be
        unloaded from the Dataset object. Once all done, the Dataset will be
        pickled on disk.

        """
        # * Confirm the saving if needed and create the directory.
        if force == False and self.dir == self.dirsave:
            l.LOGGER.warning("Try to overwrite the loaded dataset by saving a new one?")
            confirm = input("Press [ENTER] to continue, C^c to abort!")
        self.create_dirsave()
        # * Save the inputs of training set and unload if asked.
        if self.train_set is not None:
            self.train_set.dump_input(unload=unload)
            if unload is True:
                self.train_set.unload_trace()
        # * Save the inputs of attack set and unload if asked.
        if self.attack_set is not None:
            self.attack_set.dump_input(unload=unload)
            if unload is True:
                self.attack_set.unload_trace()
        # * Save the Dataset object once heavy data has been unloaded.
        with open(self.get_path(save=True), "wb") as f:
             pickle.dump(self, f)
             if log is True:
                 l.LOGGER.info("Dataset saved to '{}'".format(self.get_path(save=True)))

    def add_subset(self, name, subtype, input_gen, input_src, nb_trace_wanted=0):
        subset = Subset(self, name, subtype, input_gen, input_src, nb_trace_wanted)
        if subtype == SubsetType.TRAIN:
            self.train_set = subset
        elif subtype == SubsetType.ATTACK:
            self.attack_set = subset

    def add_profile(self):
        self.profile = Profile(self)

    def get_subset(self, id):
        """Return a subset. ID can be a string representing the name of the
        subset, or a SubsetType representing the type of the subset.

        """
        if isinstance(id, str):
            if id == self.train_set.name:
                return self.train_set
            elif id == self.attack_set.name:
                return self.attack_set
        elif id in SubsetType:
            if id == SubsetType.TRAIN:
                return self.train_set
            elif id == SubsetType.ATTACK:
                return self.attack_set

    def get_profile(self):
        # Can be None.
        return self.profile

    def is_able_to_instrument(self, subset, idx):
        """Check if this dataset is able to be instrumented for the specified configuration.

        :param subset: Subset reference.

        :param idx: Instrumentation index.

        """
        # Check length of inputs based on initialized data.
        if idx >= len(subset.pt) and subset.input_gen != InputGeneration.RUN_TIME:
            raise Exception("Requested index {} is higher than plaintext array of subset '{}'!".format(idx, subset.name))
        if idx >= len(subset.ks) and subset.input_gen != InputGeneration.RUN_TIME:
            raise Exception("Requested index {} is higher then keys array of subset '{}'!".format(idx, subset.name))

class Subset():
    """Train or attack subset."""

    # Runtime variable for new inputs.
    # Set to False at initialization and when saving inputs on disk.
    # Set to True when inserting a new input at run time.
    run_new_input = False

    def __init__(self, dataset, name, subtype, input_gen, input_src, nb_trace_wanted = 0):
        assert subtype in SubsetType, "Bad subset type!"
        assert input_gen in InputGeneration, "Bad input generation value!"
        assert input_src in InputSource if input_gen == InputGeneration.RUN_TIME else True, "Bad input source method when generation is set to RUN_TIME!"
        self.dataset = dataset
        self.name = name
        self.subtype = subtype
        self.input_gen = input_gen
        self.input_src = input_src
        self.nb_trace_wanted = nb_trace_wanted
        self.load_trace_idx = None
        self.nf = None
        self.ff = None
        self.template = None
        self.bad_entries = []
        if self.input_gen == InputGeneration.INIT_TIME and nb_trace_wanted < 1:
            l.LOGGER.error("initialization of plaintexts and keys at init time using {} traces is not possible!".format(nb_trace_wanted))
            raise Exception("initilization of subset failed!")
        self.init_subset_type()
        self.init_input()

    def init_subset_type(self):
        assert(self.subtype in SubsetType)
        if self.subtype == SubsetType.TRAIN:
            self.dir = "train"
            self.pt_type = InputType.VARIABLE
            self.ks_type = InputType.VARIABLE
        elif self.subtype == SubsetType.ATTACK:
            self.dir = "attack"
            self.pt_type = InputType.VARIABLE
            self.ks_type = InputType.FIXED

    # NOTE: The get_trace_from_disk() is a modified copy of this function.
    def load_trace(self, idx=-1, nf=True, ff=True, check=False, start_point=0, end_point=0, log=False, custom_dtype=True):
        """Load the on-disk traces into memory.

        The loading will put the traces in the self.nf and self.ff
        variables. For scripting conveniance, the functions also returns
        references to the loaded trace(s) in a tuple composed of (self.nf,
        self.ff). The trace(s) are loaded into a 2D np.ndarray.

        IDX can be -1 for all traces, an INT for a specific trace index, or
        a RANGE for a range of traces. If using a RANGE, please use range(0, x)
        with x > 1. NF and FF can be set to False to not load them in an
        unpacked dataset.

        Traces truncation during loading can be achieved using START_POINT and
        END_POINT. If START_POINT is set to different from 0, use it as start index
        during loading the traces. If END_POINT is set to different from 0, use it
        as end index during loading the traces.

        :param log: Set to True to enable logging.

        """
        if log is True:
            l.LOGGER.info("Load traces (nf={}, ff={}) from {} subset...".format(nf, ff, self.name))
        assert(path.exists(self.get_path()))
        if isinstance(idx, int) and idx == -1:
            self.nf, self.ff = load.load_all_traces(self.get_path(), nf_wanted=nf, ff_wanted=ff, start_point=start_point, end_point=end_point, custom_dtype=custom_dtype)
        elif isinstance(idx, int):
            self.nf, self.ff = load.load_pair_trace(self.get_path(), idx, nf=nf, ff=ff, custom_dtype=custom_dtype)
            self.nf[0] = None if self.nf[0] is None else load.truncate(self.nf[0], start_point, end_point)
            self.ff[0] = None if self.ff[0] is None else load.truncate(self.ff[0], start_point, end_point)
        elif isinstance(idx, range):
            self.nf, self.ff = load.load_all_traces(self.get_path(), start=idx.start, stop=idx.stop, nf_wanted=nf, ff_wanted=ff, start_point=start_point, end_point=end_point, custom_dtype=custom_dtype)
        # Search for bad entries and set them to 0.
        # NOTE: Otherwise, we can load traces of different shape, even empty (0).
        # Then, the load.reshape function would reshape all traces to 0.
        ff_bad = load.find_bad_entry(self.ff, ref_size=len(self.ff[0]), log=log)
        for v in ff_bad:
            _, self.ff[v] = analyze.fill_zeros_if_bad(self.ff[0], self.ff[v], log=True, log_idx=v)
        # NOTE: Always return 2D np.ndarray.
        self.nf = utils.list_array_to_2d_array(self.nf)
        self.ff = utils.list_array_to_2d_array(self.ff)
        self.load_trace_idx = idx
        if check is True:
            if nf is True and self.nf is None:
                raise Exception("Can't load NF trace!")
            if ff is True and self.ff is None:
                raise Exception("Can't load FF trace!")
        # Check dimensions.
        assert self.nf is None or self.nf.ndim == 2
        assert self.ff is None or self.ff.ndim == 2
        return self.nf, self.ff

    # NOTE: This function is a modified copy of the load_trace() function. It
    # should be worth to refactor the twos to use get_trace_from_disk() inside
    # load_trace().
    def get_trace_from_disk(self, idx=-1, nf=True, ff=True, check=False, start_point=0, end_point=0, custom_dtype=True):
        """Get a trace from the disk without altering the Dataset object.

        Compared from the load_trace() function, which is used to load one or a
        bunch of trace(s) in dataset for a further processing, this function
        will only return a trace from the disk without changing the loaded
        traces inside the dataset.

        For the parameters and the returned objects, refers to the load_trace()
        function.

        """
        assert(path.exists(self.get_path()))
        if isinstance(idx, int) and idx == -1:
            load_nf, load_ff = load.load_all_traces(self.get_path(), nf_wanted=nf, ff_wanted=ff, start_point=start_point, end_point=end_point, custom_dtype=custom_dtype)
        elif isinstance(idx, int):
            load_nf, load_ff = load.load_pair_trace(self.get_path(), idx, nf=nf, ff=ff, custom_dtype=custom_dtype)
            load_nf[0] = None if load_nf[0] is None else load.truncate(load_nf[0], start_point, end_point)
            load_ff[0] = None if load_ff[0] is None else load.truncate(load_ff[0], start_point, end_point)
        elif isinstance(idx, range):
            load_nf, load_ff = load.load_all_traces(self.get_path(), start=idx.start, stop=idx.stop, nf_wanted=nf, ff_wanted=ff, start_point=start_point, end_point=end_point, custom_dtype=custom_dtype)
        # NOTE: Always return 2D np.ndarray.
        load_nf = utils.list_array_to_2d_array(load_nf)
        load_ff = utils.list_array_to_2d_array(load_ff)
        if check is True:
            if nf is True and load_nf is None:
                raise Exception("Can't load NF trace!")
            if ff is True and load_ff is None:
                raise Exception("Can't load FF trace!")
        # Check dimensions.
        assert load_nf is None or load_nf.ndim == 2
        assert load_ff is None or load_ff.ndim == 2
        return load_nf, load_ff

    def unload_trace(self):
        """Delete and forget references about any loaded trace(s) from disk."""
        self.load_trace_idx = None
        del self.nf
        self.nf = None
        del self.ff
        self.ff = None

    def save_trace(self, nf=True, ff=True, custom_dtype=True):
        if isinstance(self.load_trace_idx, int) and self.load_trace_idx == -1:
            load.save_all_traces(self.get_path(save=True),
                                 self.nf if nf is True else None,
                                 self.ff if ff is True else None,
                                 packed=False,
                                 custom_dtype=custom_dtype)
        elif isinstance(self.load_trace_idx, int) and self.load_trace_idx > -1:
            load.save_pair_trace(self.get_path(save=True), self.load_trace_idx,
                                 self.nf[0] if nf is True else None,
                                 self.ff[0] if ff is True else None,
                                 custom_dtype=custom_dtype)
        elif isinstance(self.load_trace_idx, range):
            load.save_all_traces(self.get_path(save=True),
                                 self.nf if nf is True else None,
                                 self.ff if ff is True else None,
                                 packed=False, start=self.load_trace_idx.start, stop=self.load_trace_idx.stop,
                                 custom_dtype=custom_dtype)
        self.unload_trace()

    def get_save_trace_exist(self, idx=-1):
        idx = idx if idx > -1 else self.load_trace_idx
        return load.is_dataset_unpacked(self.get_path(save=True), idx)

    def load_input(self):
        if path.exists(self.get_path()):
            self.pt = load.load_plaintexts(self.get_path())
            self.ks = load.load_keys(self.get_path())

    def dump_input(self, unload=True):
        assert(path.exists(self.get_path()))
        # NOTE: We could add a mechanism here to only save if needed using the
        # self.run_new_input flag.
        # * Save plaintext input.
        if self.pt is not None:
            load.save_plaintexts(self.get_path(save=True), self.pt)
            if unload is True:
                del self.pt
                self.pt = None
        # * Save key input.
        if self.ks is not None:
            load.save_keys(self.get_path(save=True), self.ks)
            if unload is True:
                del self.ks
                self.ks = None
        # * Turn runtime dirty flags OFF.
        self.run_new_input = False

    def prune_input(self, save=False):
        self.ks = load.prune_entry(self.ks, range(self.get_nb_trace_ondisk(save=save), len(self.ks)))
        self.pt = load.prune_entry(self.pt, range(self.get_nb_trace_ondisk(save=save), len(self.pt)))

    def init_input(self):
        assert(self.input_gen in InputGeneration)
        assert(self.pt_type in InputType and self.ks_type in InputType)
        self.pt = []
        self.ks = []
        # Handle generation at initialization time.
        if self.input_gen == InputGeneration.INIT_TIME:
            self.init_input_init_time()
        elif self.input_gen == InputGeneration.RUN_TIME:
            self.init_input_run_time()

        # NOTE: np.uint8 is important to specify here because of the
        # ".tobytes()" function used in "lib/utils.py". It is the size of each
        # array element which is 1 byte.
        self.pt = np.asarray(self.pt, dtype=np.uint8)
        self.ks = np.asarray(self.ks, dtype=np.uint8)

    def init_input_run_time(self):
        """Initialize the input storage based on the number of wanted traces.

        It is meant to be set later using set_current_ks() and set_current_pt()
        functions.

        """
        pt_nb = self.nb_trace_wanted if self.pt_type == InputType.VARIABLE else 1
        ks_nb = self.nb_trace_wanted if self.ks_type == InputType.VARIABLE else 1
        self.pt = np.zeros((pt_nb, 16), dtype=np.uint8)
        self.ks = np.zeros((ks_nb, 16), dtype=np.uint8)
        # NOTE: To double the size of an input array, one can use:
        # self.pt = np.concatenate((self.pt, np.zeros(self.pt.shape, dtype=np.uint8)))

    def init_input_init_time(self):
        """Generate the input when InputGeneration has been set to INIT_TIME."""
        assert(self.input_gen == InputGeneration.INIT_TIME)
        if self.subtype == SubsetType.TRAIN:
            generator = input_generators.balanced_generator
        elif self.subtype == SubsetType.ATTACK:
            generator = input_generators.unrestricted_generator
        if self.pt_type == InputType.VARIABLE and self.ks_type == InputType.FIXED:
            self.ks = [generator(length=16).__next__()]
            for plaintext in generator(length=16, bunches=256):
                if len(self.pt) == self.nb_trace_wanted:
                    break
                self.pt.append(plaintext)
            assert(len(self.pt) == self.nb_trace_wanted)
            assert(len(self.ks) == 1)
        elif self.pt_type == InputType.VARIABLE and self.ks_type == InputType.VARIABLE:
            for key in generator(length=16):
                for plaintext in generator(length=16):
                    if len(self.pt) == self.nb_trace_wanted:
                        break
                    self.ks.append(key)
                    self.pt.append(plaintext)
                if len(self.pt) == self.nb_trace_wanted:
                    break
            assert(len(self.pt) == len(self.ks))
            assert(len(self.pt) == self.nb_trace_wanted)

    def get_nb_trace_ondisk(self, save=False):
        return load.get_nb(self.get_path(save))

    def get_path(self, save=False):
        """Return the full path of the subset. Must be dynamic since the full
        path of the dataset can change since its creation when pickling it.

        """
        return path.join(self.dataset.dir if not save else self.dataset.dirsave, self.dir)

    def replace_trace(self, sig, typ):
        """Replace traces with new one(s).

        This function has to be used when we want to register new trace(s) in a
        subset which doesn't have the same shape, for example after a
        processing (e.g. extraction). Hence, all previously contained traces
        will be replaced by the new one(s).

        SIG is a ND np.ndarray containing trace(s), TYP is a TraceType.[NF|FF]
        to choose between near-field trace or far-field trace.

        """
        if typ == TraceType.NF:
            del self.nf
            self.nf = np.array(sig, ndmin=2)
        elif typ == TraceType.FF:
            del self.ff
            self.ff = np.array(sig, ndmin=2)

    def __str__(self):
        string = "subset '{}':\n".format(self.name)
        string += "- dir: {}\n".format(self.dir)
        string += "- get_path(save=False): {}\n".format(self.get_path(save=False))
        string += "- get_path(save=True): {}\n".format(self.get_path(save=True))
        if self.nf is not None:
            assert(type(self.nf) == np.ndarray)
            string += "- loaded near-field trace shape is {}\n".format(self.nf.shape)
        if self.ff is not None:
            assert(type(self.ff) == np.ndarray)
            string += "- loaded far-field trace shape is {}\n".format(self.ff.shape)
        if self.ks is not None:
            assert(type(self.ks) == np.ndarray)
            string += "- loaded keys shape is {}\n".format(self.ks.shape)
        if self.pt is not None:
            assert(type(self.pt) == np.ndarray)
            string += "- loaded plaintexts shape is {}\n".format(self.pt.shape)
        if self.load_trace_idx is not None:
            string += "- loaded trace idx: {}\n".format(self.load_trace_idx)
        if self.template is not None:
            string += "- template shape: {}\n".format(self.template.shape)
        string += "- on-disk number of traces is {}\n".format(self.get_nb_trace_ondisk())
        string += "- bad entries are {}\n".format(self.bad_entries)
        return string

    def get_current_ks(self, idx):
        """Get the current key based on an recording index.

        If the input type is set to InputType.FIXED, always return the key at
        index 0.

        Assert for correct index bounds.

        """
        assert self.ks_type == InputType.FIXED or (idx >= 0 and idx < len(self.ks))
        return self.ks[0 if self.ks_type == InputType.FIXED else idx]

    def get_current_pt(self, idx):
        """Get the current plaintext based on an recording index.

        If the input type is set to InputType.FIXED, always return the
        plaintext at index 0.

        Assert for correct index bounds.

        """
        assert self.pt_type == InputType.FIXED or (idx >= 0 and idx < len(self.pt))
        return self.pt[0 if self.pt_type == InputType.FIXED else idx]

    def set_current_ks(self, idx, val):
        """Set the current plaintext to VAL based on recording index IDX.

        If the input type is set to InputType.FIXED, only set the plaintext if
        IDX is equal to 0, otherwise silently discard it.

        Assert for correct index bounds and input generation method.

        """
        assert self.ks_type == InputType.FIXED or (idx >= 0 and idx < len(self.ks))
        assert self.input_gen == InputGeneration.RUN_TIME
        if self.ks_type == InputType.FIXED and idx != 0 :
            return
        else:
            l.LOGGER.debug("Set subset {} ks index #{}: 0x{} / {}".format(self.subtype, idx, utils.npy_int_to_str_hex(val), val))
            self.ks[idx] = val
            self.run_new_input = True
            
    def set_current_pt(self, idx, val):
        """Set the current key to VAL based on recording index IDX.

        If the input type is set to InputType.FIXED, only set the key if IDX is
        equal to 0, otherwise silently discard it.

        Assert for correct index bounds and input generation method.

        """
        assert self.pt_type == InputType.FIXED or (idx >= 0 and idx < len(self.pt))
        assert self.input_gen == InputGeneration.RUN_TIME
        if self.pt_type == InputType.FIXED and idx != 0 :
            return
        else:
            l.LOGGER.debug("Set subset {} pt index #{}: 0x{} / {}".format(self.subtype, idx, utils.npy_int_to_str_hex(val), val))
            self.pt[idx] = val
            self.run_new_input = True

    def overview(self, base = 0, nb = 5, plot = 0, custom_dtype=True):
        """Overview a small numbers of Far-Field (FF) traces from the subset.

        :param base: Index of first trace to plot.

        :param nb: Number of traces plot.

        :param plot: Select the plot type [0 = plot_time_spec_sync_axis | 1 = plot_time_overwrite]

        """
        # Load the requested trace.
        self.load_trace(range(base, base + nb), nf=False, ff=True, custom_dtype=custom_dtype)
        assert self.ff is not None and type (self.ff) == np.ndarray and self.ff.ndim == 2
        # Select the plotting function and plot the loaded traces.
        if plot == 0:
            libplot.plot_time_spec_sync_axis(self.ff, xtime=False)
        elif plot == 1:
            libplot.plot_time_overwrite(self.ff)
        assert plot == 0 or plot == 1, "Bad plot selection!"

    @staticmethod
    # NOTE: Untested method. Can be used inside Dataset.get_subset().
    def get_subtype_from_str(subtype):
        """Get the SubsetType enumeration value from a string."""
        assert subtype == "train" or subtype == "attack", "Bad subtype string!"
        if subtype == "train":
            return SubsetType.TRAIN
        elif subtype == "attack":
            return SubsetType.ATTACK
        
class Profile():
    # Reference to the parent dataset (used to resolve path). Can be None.
    dataset = None
    # Name of the (sub)directory containing the profile if linked to a parent
    # dataset.
    dir = None
    # Full path of the profile directory. Arbitrary if not linked to a parent
    # dataset, otherwise set according to self.dataset.dir.
    fp = None

    # Profile's filenames.
    POIS_FN       = "POIS.npy"
    RS_FN         = "PROFILE_RS.npy"
    RZS_FN        = "PROFILE_RZS.npy"
    MEANS_FN      = "PROFILE_MEANS.npy"
    STDS_FN       = "PROFILE_STDS.npy"
    COVS_FN       = "PROFILE_COVS.npy"
    MEAN_TRACE_FN = "PROFILE_MEAN_TRACE.npy"

    # Profile's data.
    POIS        = None
    RS          = None
    RZS         = None
    MEANS       = None
    STDS        = None
    COVS        = None
    MEAN_TRACE  = None
    # Starting point used in original trace.
    POINT_START = None
    # Ending point used in original trace.
    POINT_END   = None
    
    def __init__(self, dataset = None, fp = None):
        """Initialize a profile.

        Set EITHER the DATASET parameter to a Dataset reference or the FP
        parameter to a full valid path.

        """
        # Safety-check of using either DATASET or FP.
        assert fp is None if dataset is not None else True
        assert dataset is None if fp is not None else True

        # Attach a dataset if needed.
        if dataset is not None:
            self.attach_dataset(dataset)
        # Attach a full path if needed.
        elif fp is not None:
            self.attach_path(fp)

    def get_path(self, save=False, fp=False):
        """Return the absolute path of the Profile.

        Assert that the dirname of the returned path exists. If FP is set to
        True, force returning path based on self.fp.

        """
        # If a dataset is attached to the profile, return a path based on the
        # dataset path.
        if self.dataset is not None and fp is False:
            assert self.dataset.dir is not None and path.exists(self.dataset.dir)
            return path.abspath(path.join(self.dataset.dir, self.dir))
        # If a full path is registered, return it.
        elif self.fp is not None or fp is True:
            assert path.exists(path.dirname(self.fp))
            return path.abspath(self.fp)
        else:
            assert False, "Profile has not been configured correctly!"

    def save(self, full_path=None):
        """Store traces and points from the Profile."""
        # NOTE: Feature to test.
        fp = False
        # if full_path is not None:
        #     self.fp = path.abspath(full_path)
        #     fp = True
        os.makedirs(self.get_path(fp=fp), exist_ok=True)
        np.save(path.join(self.get_path(fp=fp), Profile.POIS_FN), self.POIS)
        np.save(path.join(self.get_path(fp=fp), Profile.RS_FN), self.RS)
        np.save(path.join(self.get_path(fp=fp), Profile.RZS_FN), self.RZS)
        np.save(path.join(self.get_path(fp=fp), Profile.MEANS_FN), self.MEANS)
        np.save(path.join(self.get_path(fp=fp), Profile.STDS_FN), self.STDS)
        np.save(path.join(self.get_path(fp=fp), Profile.COVS_FN), self.COVS)
        np.save(path.join(self.get_path(fp=fp), Profile.MEAN_TRACE_FN), self.MEAN_TRACE)

    # Load the profile, for comparison or for attacks.
    def load(self):
        self.POIS       = np.load(path.join(self.get_path(), Profile.POIS_FN))
        self.RS         = np.load(path.join(self.get_path(), Profile.RS_FN))
        self.RZS        = np.load(path.join(self.get_path(), Profile.RZS_FN))
        self.MEANS      = np.load(path.join(self.get_path(), Profile.MEANS_FN))
        self.COVS       = np.load(path.join(self.get_path(), Profile.COVS_FN))
        self.STDS       = np.load(path.join(self.get_path(), Profile.STDS_FN))
        self.MEAN_TRACE = np.load(path.join(self.get_path(), Profile.MEAN_TRACE_FN))

    def plot(self, delim=False, save=None, plt_param_dict={}):
        # Code taken from attack.py:find_pois().
        # Plot the POIs.
        plt.subplots_adjust(hspace = 1)
        plt.subplot(2, 1, 1)
        plt.xlabel("Samples")
        plt.ylabel("Correlation coeff. (r)")
        for i, snr in enumerate(self.RS):
            plt.plot(snr, label="subkey %d"%i, **plt_param_dict)
        for bnum in range(16):
            plt.plot(self.POIS[bnum], self.RS[bnum][self.POIS[bnum]], '.')
        # Plot the mean trace.
        plt.subplot(2, 1, 2)
        plt.plot(self.MEAN_TRACE, **plt_param_dict)
        plt.xlabel("Samples")
        plt.ylabel("Mean trace")
        plt.tight_layout()
        if save is None:
            plt.show()
        else:
            plt.savefig(save)

        # Advanced plot by printing the delimiters using the FF trace #0.
        # NOTE: This part imply that profile has been built with FF and not NF.
        if delim is not False and self.dataset.train_set.get_nb_trace_ondisk() > 0:
            if self.dataset.train_set.ff is None:
                self.dataset.train_set.load_trace(0, nf=False, ff=True, check=True)
            libplot.plot_time_spec_sync_axis(self.dataset.train_set.ff[0:1], samp_rate=self.dataset.samp_rate, peaks=[self.POINT_START, self.POINT_END])
   
    def __str__(self):
        string = "profile:\n"
        string += "- dataset: {}\n".format(self.dataset is not None)
        string += "- dir: {}\n".format(self.dir)
        string += "- fp: {}\n".format(self.fp)
        string += "- get_path(): {}\n".format(self.get_path())
        if self.POIS is not None:
            string += "- pois shape: {}\n".format(self.POIS.shape)
        if self.MEAN_TRACE is not None:
            string += "- profile trace shape: {}\n".format(self.MEAN_TRACE.shape)
        if self.POINT_START:
            string += "- profile start point: {}\n".format(self.POINT_START)
        if self.POINT_END:
            string += "- profile end point: {}\n".format(self.POINT_END)
        return string

    def attach_dataset(self, dataset):
        """Attach a dataset to the Profile."""
        assert dataset is not None and type(dataset) == Dataset
        assert self.dataset is None, "Cannot attach a new dataset while a dataset is still attached!"
        assert self.fp is None, "Cannot attach a new dataset while a full path is already set!"
        self.dir = "profile"   # Fixed subdirectory.
        self.dataset = dataset # Parent. Don't need to save the subset as the
                               # subset is always train for a profile.

    def attach_path(self, fp):
        """Attach a full path to the Profile."""
        assert path.exists(fp)
        assert self.dataset is None, "Cannot attach a new path while a dataset is still attached!"
        assert self.fp is None, "Cannot attach a new path while a full path is already set!"
        self.fp = fp

class DatasetProcessing():
    """Processing workflow using a Dataset.

    Allows to use friendly interface functions for scripting and advanced
    processing functions (e.g. parallelization).

    Functions:
    - resume: Resume from previous processing.
    - create: Create a new processing.
    - process: Execute the previously created processing.
    - disable_plot: Disable the plot(s) for next processing.
    - disable_parallel: Disable the processing parallelization.
    - restore_parallel: Restore the previous processing parallelization.
    - is_parallel: To know if parallelization is enabled.

    """

    # * List all public variables.
    # Dataset of the processing.
    # NOTE: Mandatory to not be None.
    dset = None
    # Subset of the processing.
    # NOTE: Mandatory to not be None.
    sset = None
    # Index of start trace for the processing.
    start = 0
    # Index of stop trace for the processing (-1 means infinite).
    stop = -1
    # Processing title.
    process_title = None
    # Processing function. Must have this signature:
    # FUNC_NAME(dset, sset, plot, args)
    # Where the FUNC_NAME can get supplementary arguments from the ARGS list/tuple.
    process_fn = None
    # Processing plotting switch (a PlotOnce class).
    process_plot = None
    # Processing function arguments.
    process_args = None
    # Processing number of workers.
    # < 0 = maximum available processes.
    # > 0 = specified number of processes.
    # 0 = no process, run sequentially.
    process_nb = None
    _process_nb = None # Backup.

    def __init__(self, indir, subset, outdir=None, stop=-1):
        """Initialize a dataset processing.

        Load a dataset from INDIR and load the SUBSET subset. If OUTDIR is not
        None, set it as the savedir of the dataset. On error during the dataset
        loading, quit the programm.

        """
        # Install the signal handler.
        self.__signal_install()
        # Get dataset and subset.
        self.dset = Dataset.pickle_load(indir, quit_on_error=True)
        self.sset = self.dset.get_subset(subset)
        assert self.dset is not None and self.sset is not None
        # Set the outdir directory for saving.
        if outdir is not None:
            self.dset.set_dirsave(outdir)
        # Set stop trace.
        if stop == -1:
            self.stop = self.sset.get_nb_trace_ondisk()
        else:
            self.stop = stop
        # Set the dirty flag to True after loading.
        self.dset.dirty = True

    def resume(self, from_zero=False):
        """Resume the processing of a dataset...

        If:
        - The FROM_ZERO parameter is set to False.
        - The DIRTY flag of the previsouly saved dataset is set to True.

        By:
        1. Fetching the template previously saved.
        2. Fetching the bad entries previously saved.
        3. Using the dirty idx previously saved as start index.

        """
        if from_zero is False and self.dset.get_savedir_dirty():
            self.dset.resume_from_savedir(self.sset.subtype)
            self.start = self.dset.dirty_idx
            l.LOGGER.info("Resume at trace {} using template from previous processing".format(self.start))
            l.LOGGER.debug("Template: shape={}".format(self.sset.template.shape))

    def create(self, title, fn, plot, args, nb = -1):
        """Create a processing.

        The processing will be titled TITLE, running the function FN using the
        plot switch PLOT and custom arguments ARGS.

        If NB is set to negative number, use the maximum number of workers. If
        set to a positive number, use this as number of workers. If set to 0,
        disable multi-process processing and use a single-process processing.

        """
        assert isinstance(plot, libplot.PlotOnce), "plot parameter must be a PlotOnce class!"
        self.process_title = title
        self.process_fn = fn
        self.process_plot = plot
        self.process_args = args
        if nb < 0:
            self.process_nb = os.cpu_count() - 1
            l.LOGGER.info("Automatically select {} processes for parallelization".format(self.process_nb))
        else:
            self.process_nb = nb
        self._process_nb = self.process_nb

    def process(self):
        """Run the (parallelized) processing.

        The processing must be configured using DatasetProcessing.create()
        before to use this function.

        """
        # Check that self.create() function has been called.
        assert self.process_title is not None
        assert self.process_fn is not None
        assert self.process_plot is not None
        assert self.process_args is not None
        assert self.process_nb >= 0
        
        def _init(i, stop):
            """Initialize the processing starting at trace index I.

            Return a tuple composed of the Queue for result transfer and a list
            of processes.

            """
            # NOTE: The first processing needs to be executed in the main
            # process to modify the dataset object. Remaning processings could
            # rely on this one to get some parameters (e.g. the template
            # signal).
            self.disable_parallel(i == 0)
            # Queue for transferring results from processing function (parallelized or not).
            q = Queue()
            # List of processes. Only create necessary processes.
            ps_len = self.process_nb
            if i + self.process_nb >= stop:
                ps_len -= i + self.process_nb - stop
            ps = [None] * ps_len
            # Initialize the processes if needed (but do not run them).
            for idx, _ in enumerate(ps):
                l.LOGGER.debug("Create process index #{} for trace index #{}".format(idx, i + idx))
                ps[idx] = Process(target=self.__process_fn, args=(q, self.dset, self.sset, i + idx, self.process_plot.pop(), self.process_args,))
            return q, ps

        def _run(i, q, ps):
            """Run the processing starting at trace index I using the Queue Q
            and the processes of list PS.

            """
            # Create the processes and perform the parallelized processing...
            if self.is_parallel():
                for idx, proc in enumerate(ps):
                    proc.start()
                    l.LOGGER.debug("Started process: idx={}".format(idx))
            # ...or perform process sequentially.
            else:
                self.__process_fn(q, self.dset, self.sset, i, self.process_plot.pop(), self.process_args)

        def _get(q, ps):
            """Get the processing results using the Queue Q and the processes of list PS."""
            # Check the result.
            for _, __ in enumerate(ps):
                l.LOGGER.debug("Wait result from queue...")
                check, i_processed = q.get()
                if check is True:
                    self.sset.bad_entries.append(i_processed)

        def _end(i_done, ps, pbar=None):
            """Terminate the processing for trace index I_DONE.

            1. Update the processing loop information to prepare the next
               processing.
            2. Save the processing state in the dataset for further
               resuming.
            3. If parallelized, terminated the processing contained in
               the PS list.
            4. If specified, update TQDM's PBAR just like index I_DONE.

            Return the new index I for next processing.

            """
            # Terminate the processes.
            for idx, proc in enumerate(ps):
                l.LOGGER.debug("Join process... idx={}".format(idx))
                proc.join()
            # Update the progress index and bar.
            # NOTE: Handle case where process_nb == 0 for single-process processing.
            i_step = len(ps) if self.process_nb > 0 else 1
            i = i_done + i_step
            pbar.update(i_step)
            # Save dataset for resuming if not finishing the loop.
            self.dset.dirty_idx = i
            self.dset.pickle_dump(unload=False, log=False)
            # Restore parallelization after first trace processing if needed.
            # NOTE: Should be at the end since it will modify self.process_nb.
            self.restore_parallel(i_done == 0)
            l.LOGGER.debug("Finished processing: trace #{} -> #{}".format(i_done, i - 1))
            return i
            
        # Setup progress bar.
        with (logging_redirect_tqdm(loggers=[l.LOGGER]),
              tqdm(initial=self.start, total=self.stop, desc=self.process_title) as pbar,):
            i = self.start
            while i < self.stop:
                # Initialize processing for trace(s) starting at index i.
                q, ps = _init(i, self.stop)
                # Run the processing.
                _run(i, q, ps)
                # Get and check the results.
                _get(q, ps)
                # Terminate the processing.
                i = _end(i, ps, pbar=pbar)

    def disable_plot(self, cond=True):
        """Disable the plotting parameter if COND is True."""
        if cond is True and self.process_plot is True:
            l.LOGGER.debug("Disable plotting for next processings")
            self.process_plot = False

    def disable_parallel(self, cond=True):
        """Disable the parallel processing if COND is True.

        One can call restore_parallel() to restore the previous parallelization
        value.

        """
        if cond is True and self.is_parallel() is True:
            l.LOGGER.debug("Disable parallelization for next processings")
            self._process_nb = self.process_nb
            self.process_nb = 0

    def restore_parallel(self, cond=True):
        """Restore the process parallelization as before disable_parallel()
        call if COND is True.

        """
        if cond is True and self.is_parallel(was=True):
            l.LOGGER.debug("Restore previous parallelization value for next processings")
            self.process_nb = self._process_nb

    def is_parallel(self, was=False):
        """Return True if parallelization is enabled, False otherwise.

        Set WAS to True to test against the value before the disable_parallel()
        call.

        """
        return self.process_nb > 0 if was is False else self._process_nb > 0

    def __signal_install(self):
        """Install the signal handler.

        Catch the SIGINT signal.

        """
        global DPROC
        DPROC = self
        signal.signal(signal.SIGINT, self.__signal_handler)

    def __process_fn(self, q, dset, sset, i, plot, args):
        """Main function for processes.

        It is usually ran by a caller process from the self.process/_run()
        function. It may be run in the main proces too. It will load a trace,
        execute the processing based on the self.process_fn function pointer,
        may check and plot the result, and save the resulting trace.

        Q is a Queue to transmit the results, DSET a Dataset, SSET a Subset, I
        the trace index to load and process, PLOT a flag indicating to plot the
        result, and ARGS additionnal arguments transmitted to the
        self.process_fn function.

        """
        l.LOGGER.debug("Start __process_fn() for trace #{}...".format(i))
        # * Load the trace to process.
        # NOTE: We choose to always load traces one by one since raw traces can
        # be large (> 30 MB).
        sset.load_trace(i, nf=False, ff=True, check=True, log=False)
        # * Apply the processing and get the resulting trace.
        # NOTE: ff can be None if the processing fails.
        ff = self.process_fn(dset, sset, plot, args)
        # * Check the trace is valid.
        check = False
        if i > 0:
            check, ff_checked = analyze.fill_zeros_if_bad(sset.template, ff, log=True, log_idx=i)
        elif i == 0 and ff is not None:
            l.LOGGER.info("Trace #0 processing (e.g. creating a template) is assumed to be valid!")
            ff_checked = ff
        else:
            raise Exception("Trace #0 processing encountered an error!")
        sset.replace_trace(ff_checked, TraceType.FF)
        # * Plot the averaged trace if wanted and processing succeed.
        if sset.ff[0] is not None:
            libplot.plot_time_spec_sync_axis(sset.ff[0:1], samp_rate=dset.samp_rate, cond=plot, comp=complex.CompType.AMPLITUDE)
        # * Save the processed trace and transmit result to caller process.
        sset.save_trace(nf=False, custom_dtype=False)
        q.put((check, i))
        l.LOGGER.debug("End __process_fn() for trace #{}".format(i))

    @staticmethod
    def __signal_handler(sig, frame):
        """Catch signal properly exiting process.

        Signal handler supposed to catch SIGINT to properly quit the processing
        by setting the stop index to 0.

        """
        global DPROC
        DPROC.stop = 0

    def __str__(self):
        """Return the __str__ from the dataset."""
        string = "dataset_processing:\n"
        string += "- start: {}\n".format(self.start)
        string += "- stop: {}\n".format(self.stop)
        return string + self.dset.__str__()
