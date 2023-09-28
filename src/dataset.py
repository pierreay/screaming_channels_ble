#!/usr/bin/python3

# FIXME: Information lost between subset for several processing.
# 
# When using two different processing commands, e.g. average for training
# subset and extralign for attack subset, then we lost information stored
# during the processing of the first subset, like the template and the bad
# entries.

from os import path
import numpy as np
from matplotlib import pyplot as plt
from scipy import signal
import click
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

import lib.debug as libdebug
import lib.analyze as analyze
import lib.load as load
import lib.device as device
import lib.log as l
import lib.plot as libplot
import lib.filters as filters
import lib.triggers as triggers
import lib.dataset as dataset

def load_dataset_or_quit(indir, subset=None, outdir=None):
    dset = dataset.Dataset.pickle_load(indir)
    sset = None
    if dset is None:
        l.LOGGER.error("dataset doesn't exists!")
        exit(-1)
    if outdir is not None:
        dset.set_dirsave(outdir)
    if subset is not None:
        sset = dset.get_subset(subset)
    dset.dirty = True
    return dset, sset

def save_dataset_and_quit(dset):
    # Disable setting dirty to False, otherwise, after a completeed averaging,
    # we are not able to extend a previous averaging.
    # dset.dirty = False
    dset.pickle_dump()
    exit(0)

@click.group(context_settings={'show_default': True})
def cli():
    """Dataset processing utility."""
    pass

@cli.command()
@click.argument("outdir", type=click.Path())
@click.argument("samp_rate", type=int)
@click.option("--input-gen-init/--no-input-gen-init", default=False,
              help="Generate plaintexts and keys at initialization instead of real-time.")
@click.option("--nb-trace-wanted-train", default=0, help="Number of wanted traces for train subset.")
@click.option("--nb-trace-wanted-attack", default=0, help="Number of wanted traces for attack subset.")
def init(outdir, samp_rate, input_gen_init, nb_trace_wanted_train, nb_trace_wanted_attack):
    """Initialize a dataset.

    Initialize a dataset in OUTDIR following given options.

    SAMP_RATE is the sampling rate used for both recording.

    """
    if path.exists(outdir):
        dset = dataset.Dataset("tmp", outdir, samp_rate)
        input_gen = dataset.InputGeneration.INIT_TIME if input_gen_init else dataset.InputGeneration.REAL_TIME
        dset.add_subset("train", dataset.SubsetType.TRAIN, input_gen, nb_trace_wanted=nb_trace_wanted_train)
        dset.add_subset("attack", dataset.SubsetType.ATTACK, input_gen, nb_trace_wanted=nb_trace_wanted_attack)
        dset.pickle_dump(force=True)
        l.LOGGER.info("save dataset in {}".format(dset.get_path(save=True)))
    else:
        l.LOGGER.error("{} doesn't exists!".format(indir))
        return 1

@cli.command()
@click.argument("indir", type=click.Path())
@click.option("--train/--no-train", default=False, help="Interrogate the train set.")
@click.option("--attack/--no-attack", default=False, help="Interrogate the attack set.")
@click.option("--pt-gen-init/--no-pt-gen-init", default=False, help="Return 1 if plaintexts were generated at initialization.")
@click.option("--ks-gen-init/--no-ks-gen-init", default=False, help="Return 1 if kets were generated at initialization.")
def query(indir, train, attack, pt_gen_init, ks_gen_init):
    """Query dataset information.

    Query an information about a dataset. Mainly used for external scripts
    (e.g. bash). Return 1 for a True answer, 0 for a False answer, -1 for an
    error. Otherwise, print the response value on the standard output.

    """
    dset = dataset.Dataset.pickle_load(indir)
    if dset is None:
        l.LOGGER.error("dataset doesn't exists!")
        exit(-1)
    subset = None
    subset = dset.train_set if train and not attack else subset
    subset = dset.attack_set if attack and not train else subset
    if subset is None:
        l.LOGGER.error("please, select a subset!")
        exit(-1)
    if pt_gen_init:
        exit(subset.pt_gen == dataset.InputGeneration.INIT_TIME)
    if ks_gen_init:
        exit(subset.ks_gen == dataset.InputGeneration.INIT_TIME)

@cli.command()
@click.argument("indir", type=click.Path())
@click.option("--outdir", type=click.Path(), default=None, help="If specified, set the outdir/savedir of the dataset.")
def debug(indir, outdir):
    """Debug a dataset.

    INDIR is the path of a directory containing a dataset.

    """
    dset, sset = load_dataset_or_quit(indir, "train", outdir)
    # * Scratchpad:
    import ipdb; ipdb.set_trace()
    print(dset)

@cli.command()
@click.argument("indir", type=click.Path())
@click.argument("outdir", type=click.Path())
@click.argument("subset", type=str)
@click.option("--nb-aes", default=1, help="Number of AES in the trace.")
@click.option("--plot/--no-plot", default=True, help="Plot a summary of the processing.")
@click.option("--template", default=-1, help="Specify template signal index to use. -1 means prompting.")
@click.option("--stop", default=1, help="Range of traces to process in the subset of the dataset. Set to -1 for maximum.")
@click.option("--force/--no-force", default=False, help="Force a restart of the processing even if resuming is detected.")
def average(indir, outdir, subset, nb_aes, plot, template, stop, force):
    """Average multiple AES executions.

    INDIR corresponds to a directory containing a dataset with traces
    containing multiple AES. For each trace, the program will search every AES
    computation and will construct a new dataset by averaging them.

    OUTDIR corresponds to the directory where the new dataset will be stored.

    SUBSET corresponds to the subset's name that will be proceed.


    """
    start = 0
    # * Load input dataset and selected subset.
    dset, sset = load_dataset_or_quit(indir, subset, outdir=outdir)
    # * Fetch template from previously saved dataset in case of resuming.
    if force is False and dset.get_savedir_dirty():
        dset.resume_from_savedir(subset)
        start = dset.dirty_idx
        l.LOGGER.info("resume at trace {} using template from previous processing".format(start))
        l.LOGGER.debug("template shape={}".format(sset.template.shape))
    # Load traces one by one since traces containing multiple AES executions
    # can be large (> 30 MB).
    with logging_redirect_tqdm(loggers=[l.LOGGER]):
        if stop == -1:
            stop = sset.get_nb_trace_ondisk()
        for i in tqdm(range(start, stop), desc="average"):
            dset.dirty_idx = i
            sset.load_trace(i)
            assert(sset.ff is not None)
            sset.ff, sset.template = analyze.average_aes(sset.ff, dset.samp_rate, nb_aes, template if sset.template is None else sset.template, plot)
            check, sset.ff = analyze.fill_zeros_if_bad(sset.template, sset.ff)
            if check is True:
                l.LOGGER.warning("error during averaging aes, trace {} filled with zeroes!".format(i))
                sset.bad_entries.append(i)
            if plot:
                libplot.plot_time_spec_share_nf_ff(sset.ff, None, dset.samp_rate)
            sset.save_trace(nf=False)
            dset.pickle_dump(unload=False, log=False)
            # * Disable plot for remainaing traces.
            plot = False
    sset.prune_input(save=True)
    save_dataset_and_quit(dset)

@cli.command()
@click.argument("indir", type=click.Path())
@click.argument("outdir", type=click.Path())
@click.argument("subset", type=str)
@click.option("--plot/--no-plot", default=True, help="Plot AES finding and template validation.")
@click.option("--offset", default=0, help="Number of samples to addition to the detected AES.")
@click.option("--length", default=10000, help="Number of samples of the window to extract.")
@click.option("--stop", default=1, help="Range of traces to process in the subset of the dataset. Set to -1 for maximum.")
@click.option("--force/--no-force", default=False, help="Force a restart of the processing even if resuming is detected.")
def extralign(indir, outdir, subset, plot, offset, length, stop, force):
    """Extract roughly the AES from RAW FF traces and align them.

    INDIR corresponds to the input dataset directory.
    OUTDIR corresponds to the output dataset directory.
    SUBSET corresponds to the subset's name that will be proceed.

    """
    start = 0
    # * Load input dataset and selected subset.
    dset, sset = load_dataset_or_quit(indir, subset, outdir=outdir)
    # * Resume from previously saved dataset if needed.
    if force is False and dset.get_savedir_dirty():
        dset.resume_from_savedir(subset)
        start = dset.dirty_idx
        l.LOGGER.info("resume at trace {} using template from previous processing".format(start))
        l.LOGGER.debug("template shape={}".format(sset.template.shape))
    if stop == -1:
        stop = sset.get_nb_trace_ondisk()
    with logging_redirect_tqdm(loggers=[l.LOGGER]):
        for i in tqdm(range(start, stop), desc="extralign"):
            # * Load trace and save current processing step in dataset.
            dset.dirty_idx = i
            sset.load_trace(i, nf=False, ff=True, check=True)
            # * Find AES and check for error.
            sset.ff = analyze.get_amplitude(sset.ff)
            starts, trigger = analyze.find_aes(sset.ff, dset.samp_rate, 1e6, 10e6, 1, lp=1e5, offset=-1.5e-4, flip=False)
            # XXX: Refactor all of the following insde the find_aes function?
            if len(starts) == 1:
                l.LOGGER.debug("number of detected aes: {}".format(len(starts)))
            else:
                l.LOGGER.error("number of detected aes is aberrant: {}".format(len(starts)))
                # If plot is ON, we are debugging/configuring or processing trace #1, hence don't continue.
                if plot:
                    libplot.plot_time_spec_share_nf_ff(sset.ff, None, dset.samp_rate, peaks=starts, triggers=trigger)
                    raise Exception("aes detection failed!")
            if plot:
                libplot.plot_time_spec_share_nf_ff(sset.ff, None, dset.samp_rate, peaks=starts, triggers=trigger)
            # * If trace 0, interactively valid the extraction as the template for further traces.
            if i == 0:
                extracted     = analyze.extract(sset.ff, starts, length=length)
                sset.template = analyze.choose_signal(extracted, -1 if plot is True else 0)
                if sset.template is None:
                    raise Exception("no choosen template signal")
            # * Align current trace against the template.
            if len(starts) == 1: # Only process if find_aes returned correctly, otherwise, set a bad trace.
                extracted = analyze.extract(sset.ff, starts, len(sset.template))
                aligned   = analyze.align(sset.template, extracted[0], dset.samp_rate, ignore=False, log=False)
            else:
                aligned = None
            # * Check the trace is valid.
            check, sset.ff = analyze.fill_zeros_if_bad(sset.template, aligned)
            if check is True:
                l.LOGGER.warning("error during processing, trace {} filled with zeroes!".format(i))
                sset.bad_entries.append(i)
            # * Save dataset for resuming if not finishing the loop.
            sset.save_trace(nf=False)
            dset.pickle_dump(unload=False, log=False)
            # * Disable plot for remainaing traces.
            plot = False
    sset.prune_input(save=True)
    save_dataset_and_quit(dset)
    
if __name__ == "__main__":
    cli()
