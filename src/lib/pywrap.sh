# * About

# Bash library wrapping my Python commands.

# * radio.py

# Initialize the radio thread in the background.
# $1 is sleeping time for radio initialization [default = 20].
# $2 is loglevel [default = DEBUG].
function radio_init() {
    ./radio.py --dir $ENVRC_RADIO_DIR --loglevel ${2-DEBUG} --config $ENVRC_CONFIG_FILE listen $ENVRC_NF_FREQ $ENVRC_FF_FREQ $ENVRC_SAMP_RATE --nf-id $ENVRC_NF_ID --ff-id $ENVRC_FF_ID --duration=$ENVRC_DURATION --gain=$ENVRC_GAIN &
    sleep ${1-20} # Wait for SDR's driver initialization.
}

# Arguments:
# $1 is loglevel [default = DEBUG].
# $2 should be the subset [train | attack] [default = train].
# $3 should be the trace recording index [default = 0].
# $4 should be any supplementary switch [EMPTY | --no-radio] [default = EMPTY]
# radio_instrument DEBUG train 0 ""
function radio_instrument() {
    # NOTE: Send a SIGINT signal such that Python goes through the __exit__()
    # of Device class, such that WHAD/Butterfly do not finish in a bad
    # state. We use a timeout of 20s in our device.py, so we use a timeout of
    # 30s here to double check it.
    timeout --signal=SIGINT 30 python3 ./radio.py --loglevel ${1-DEBUG} --dir $ENVRC_RADIO_DIR --config $ENVRC_CONFIG_FILE instrument $ENVRC_DATASET_RAW_PATH ${2-train} $ENVRC_ATTACKER_ADDR $ENVRC_VICTIM_ADDR $ENVRC_VICTIM_PORT --idx ${3-0} --config $ENVRC_DEVICE_CONFIG $4
    if [[ $? -ge 1 ]]; then
        return 1
    fi
}

# Arguments:
# $1 is --dir [default = $ENVRC_RADIO_DIR]
# $2 is SAMP_RATE [default = $ENVRC_SAMP_RATE]
# $3 is --nf-id [default = $ENVRC_NF_ID]
# $4 is --ff-id [default = $ENVRC_FF_ID]
# Default is to plot amplitude of recorded signal.
function radio_plot() {
    ./radio.py --dir ${1-$ENVRC_RADIO_DIR} --config $ENVRC_CONFIG_FILE plot ${2-$ENVRC_SAMP_RATE} --nf-id ${3-$ENVRC_NF_ID} --ff-id ${3-$ENVRC_FF_ID} --amplitude
}

# Arguments:
# $1 is log level [INFO | DEBUG] [default = DEBUG].
# $2 is plotting option [--plot | --no-plot] [default = --plot].
# $3 is overwriting option [--overwrite | --no-overwrite] [default = --no-overwrite].
# $4 is exit option [--exit-on-error | --no-exit-on-error] [default = --no-exit-on-error].
# $5: --config value.
function radio_extract() {
    # NOTE: The "-1" from test's conditions means radio disabled. It is set
    # according to the --nf-id, --ff-id, and --id specifications of the
    # "radio.py" arguments.
    # Warning about no NF-only extraction implemented.
    if [[ $ENVRC_NF_ID != -1 && $ENVRC_FF_ID == -1 ]]; then
        log_error "NF-only extraction is not implemented yet!"
    # Extract FF-only.
    elif [[ $ENVRC_NF_ID == -1 && $ENVRC_FF_ID != -1 ]]; then
        # NOTE: Same parameters as command below except no "--id".
        # NOTE: Assume frequency is FF frequency.
        ./radio.py --loglevel ${1-DEBUG} --dir $ENVRC_RADIO_DIR --config $ENVRC_CONFIG_FILE extract $ENVRC_FF_FREQ $ENVRC_SAMP_RATE $ENVRC_FF_ID ${2---plot} ${3---no-overwrite} ${4---no-exit-on-error} --config ${5-$ENVRC_EXTRACT_CONFIG}
    # Extract NF and FF based on FF extraction.
    elif [[ $ENVRC_NF_ID != -1 && $ENVRC_FF_ID != -1 ]]; then
        # NOTE: Same parameters as command above except "--id".
        ./radio.py --loglevel ${1-DEBUG} --dir $ENVRC_RADIO_DIR --config $ENVRC_CONFIG_FILE extract $ENVRC_SAMP_RATE $ENVRC_FF_ID ${2---plot} ${3---no-overwrite} ${4---no-exit-on-error} --id $ENVRC_NF_ID --config ${5-$ENVRC_EXTRACT_CONFIG}
    fi
}

function radio_quit() {
    ./radio.py quit
}

function radio_all() {
    radio_init && radio_instrument && radio_plot && radio_quit
}

# * dataset.py

# Initialize a dataset. Silently do nothing if dataset already exists. Choose
# the input generation method based on the ENVRC_DATASET_INPUT global variable.
# $1 is the dataset directory [default = $ENVRC_DATASET_RAW_PATH]
# $2 is the sample rate [default = $ENVRC_SAMP_RATE]
# $3 is the input generation method [default = --input-gen-run]
# $4 is the input source method [default = --input-src-pairing]
function dataset_init() {
    # NOTE: Dataset name is taken from lib/dataset.py/Dataset.FILENAME variable.
    if [[ ! -f ${1-$ENVRC_DATASET_RAW_PATH}/dataset.pyc ]]; then
        log_info "Initialize a dataset using the '$ENVRC_DATASET_INPUT' input generation in ${1-$ENVRC_DATASET_RAW_PATH}..."
        # Use the balanced generator to generate inputs:
        if [[ $ENVRC_DATASET_INPUT == "BALANCED" ]]; then
            ./dataset.py init ${1-$ENVRC_DATASET_RAW_PATH} ${2-$ENVRC_SAMP_RATE} ${3---input-gen-init} --nb-trace-wanted-train 65536 --nb-trace-wanted-attack 65536
        # Use the pairing to generate inputs:
        elif [[ $ENVRC_DATASET_INPUT == "PAIRING" ]]; then
            ./dataset.py init ${1-$ENVRC_DATASET_RAW_PATH} ${2-$ENVRC_SAMP_RATE} ${3---input-gen-run} ${4---input-src-pairing} --nb-trace-wanted-train 65536 --nb-trace-wanted-attack 65536
        # Use the random from Nimble to generate inputs:
        elif [[ $ENVRC_DATASET_INPUT == "SERIAL" ]]; then
            ./dataset.py init ${1-$ENVRC_DATASET_RAW_PATH} ${2-$ENVRC_SAMP_RATE} ${3---input-gen-run} ${4---input-src-serial} --nb-trace-wanted-train 65536 --nb-trace-wanted-attack 65536
        else
            log_error "Unknown input generation method!"
        fi
    else
        log_info "Dataset already initialized in ${1-$ENVRC_DATASET_RAW_PATH}!"
    fi
}

# Average the AES of single traces from one subset.
# $1 is the LOGLEVEL [default = DEBUG]
# $2 is the input dataset [default = $ENVRC_DATASET_RAW_PATH]
# $3 is the output dataset [default = $ENVRC_DATASET_AVG_PATH]
# $4 is the subset [default = train]
# $5 is the template index [default = -1 [ask]]
# $6 is the plot switch [default = --plot]
# $7 is the stop option [default = 1 [stop after first trace]]
# $8 is the force option [default = --no-force]
function dataset_average() {
    ./dataset.py --loglevel ${1-DEBUG} average --nb-aes 100 ${2-$ENVRC_DATASET_RAW_PATH} ${3-$ENVRC_DATASET_AVG_PATH} ${4-train} --template ${5--1} ${6---plot} --stop ${7-1} ${8---no-force} --jobs=-1
}

# Extract a desired AES of single traces from one subset.
# $1 is the LOGLEVEL [default = DEBUG]
# $2 is the input dataset [default = $ENVRC_DATASET_RAW_PATH]
# $3 is the output dataset [default = $ENVRC_DATASET_EXT_PATH]
# $4 is the subset [default = train]
# $5 is the template index [default = -1 [ask]]
# $6 is the plot switch [default = --plot]
# $7 is the stop option [default = 1 [stop after first trace]]
# $8 is the force option [default = --no-force]
# $9 is the index extraction option [default = 0 [first AES]]
# $10 is the window around extracted segment [default = 0, use 4000 for broad AES extraction]
function dataset_extract() {
    ./dataset.py --loglevel ${1-DEBUG} extract --nb-aes 200 ${2-$ENVRC_DATASET_RAW_PATH} ${3-$ENVRC_DATASET_EXT_PATH} ${4-train} --template ${5--1} ${6---plot} --stop ${7-1} ${8---no-force} --jobs=-1 --idx ${9-0} --window ${10-0}
}

# Debug a dataset.
# $1 is the dataset path [default = $ENVRC_DATASET_RAW_PATH].
# $2 is the subset [train | attack] [default = train].
function dataset_debug() {
    ./dataset.py debug ${1-$ENVRC_DATASET_RAW_PATH} --subset ${2-train}
}

# * attack.py

# Create a profile from a train subset.
# $1 can be --plot or --no-plot [default = --plot].
# $2 is the --dataset-path [default = $ENVRC_DATASET_AVG_PATH].
# $3 is the --num-traces [default = 0].
# $4 is the --start-point [default = 0].
# $5 is the --end-point [default = 0].
# $6 is the --num-pois [default = 2].
# $7 is the --poi-spacing [default = 2].
# $8 is the --variable [default = p_xor_k].
# $9 is the --align or --no-align [default = --no-align].
function attack_profile() {
    ./attack.py ${1---plot} --norm --dataset-path ${2-$ENVRC_DATASET_AVG_PATH} --num-traces ${3-0} --start-point ${4-0} --end-point ${5-0} profile --pois-algo r --num-pois ${6-2} --poi-spacing ${7-2} --variable ${8-p_xor_k} ${9---no-align}
}

# Run an attack from an attack subset.
# $1 can be --plot or --no-plot [default = --plot].
# $2 is the --dataset-path [default = $ENVRC_DATASET_AVG_PATH].
# $3 is the --num-traces [default = 0].
# $4 is the --start-point [default = 0].
# $5 is the --end-point [default = 0].
# $6 is the --num-pois [default = 2].
# $7 is the --poi-spacing [default = 2].
# $8 is the --variable [default = p_xor_k].
# $9 is the --align or --no-align [default = --no-align].
function attack_pcc() {
    ./attack.py ${1---plot} --norm --dataset-path ${2-$ENVRC_DATASET_AVG_PATH} --num-traces ${3-0} --start-point ${4-0} --end-point ${5-0} attack --attack-algo pcc --num-pois ${6-2} --poi-spacing ${7-2} --variable ${8-p_xor_k} ${9---no-align}
}
