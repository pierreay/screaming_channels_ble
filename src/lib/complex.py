"""Operations on complex numbers.

Functions:

- is_iq(): Is a signal composed of IQ samples.

- get_amplitude(): Get the magnitude of a signal from IQ samples.

- get_phase(): Get the phase of a signal from IQ samples.

- p2r(): Convert an IQ signal to regular/cartesian representation.

- r2p(): Convert an IQ signal to polar representation.

"""

import numpy as np
from enum import Enum

import lib.analyze as analyze

# Enumeration of components type of a signal.
CompType = Enum('CompType', ['AMPLITUDE', 'PHASE', 'PHASE_ROT'])

def is_iq(s):
    """Return True is the signal S is composed of IQ samples, False otherwise."""
    return s.dtype == np.complex64

def get_amplitude(traces):
    """Get the amplitude of one or multiples traces.

    From the TRACES 2D np.array of shape (nb_traces, nb_samples) or the 1D
    np.array of shape (nb_samples) containing IQ samples, return an array with
    the same shape containing the amplitude of the traces.

    If traces contains signals in another format than np.complex64, silently
    return the input traces such that this function can be called multiple
    times.

    """
    if traces.dtype == np.complex64:
        return np.abs(traces)
    else:
        return traces

def get_phase(traces):
    """Get the phase of one or multiples traces.

    From the TRACES 2D np.array of shape (nb_traces, nb_samples) or the 1D
    np.array of shape (nb_samples) containing IQ samples, return an array with
    the same shape containing the phase of the traces.

    If traces contains signals in another format than np.complex64, silently
    return the input traces such that this function can be called multiple
    times.

    """
    if traces.dtype == np.complex64:
        return np.angle(traces)
    else:
        return traces

def get_phase_rot(trace):
    """Get the phase of one or multiple traces."""
    if traces.dtype == np.complex64:
        return np.angle(traces)
    else:
        return traces

def get_comp(traces, comp):
    """Get a choosen component.

    Return the choosen component of signals contained in the 1D or 2D ndarray
    TRACES according to COMP set to CompType.AMPLITUDE, CompType.PHASE or
    CompType.PHASE_ROT.

    If the signals contained in TRACES are already of the given component, this
    function will do nothing.

    """
    assert type(traces) == np.ndarray, "Traces should be numpy array"
    assert (type(comp) == str or comp in CompType), "COMP is set to a bad type or bad enum value!"
    if (type(comp) == CompType and comp == CompType.AMPLITUDE) or (type(comp) == str and CompType[comp] == CompType.AMPLITUDE):
        return get_amplitude(traces)
    elif (type(comp) == CompType and comp == CompType.PHASE) or (type(comp) == str and CompType[comp] == CompType.PHASE):
        return get_phase(traces)
    elif (type(comp) == CompType and comp == CompType.PHASE_ROT) or (type(comp) == str and CompType[comp] == CompType.PHASE_ROT):
        return get_phase_rot(traces)
    assert False, "Bad COMP string!"

def is_p2r_ready(radii, angles):
    """Check if polar complex can be converted to regular complex.

    Return True if values contained in RADII and ANGLES are in the acceptable
    ranges for the P2R (polar to regular) conversion. Without ensuring this,
    the conversion may lead to aberrant values.

    RADII and ANGLES can be ND np.ndarray containing floating points values.

    """
    # Check that RADII and ANGLES are not normalized.
    norm = analyze.is_normalized(radii) or analyze.is_normalized(angles)
    # Check that 0 <= RADII <= 2^16. NOTE: RADII is computed like the following
    # with maximum value of 16 bits integers (because we use CS16 from
    # SoapySDR):
    # sqrt((2^16)*(2^16) + (2^16)*(2^16)) = 92681
    # Hence, should we use 2^17 instead?
    radii_interval = radii[radii < 0].shape == (0,) and radii[radii > np.iinfo(np.uint16).max].shape == (0,)
    # Check that -PI <= ANGLES <= PI.
    angles_interval = angles[angles < -np.pi].shape == (0,) and angles[angles > np.pi].shape == (0,)
    return not norm and radii_interval and angles_interval

def p2r(radii, angles):
    """Complex polar to regular.

    Convert a complex number from Polar coordinate to Regular (Cartesian)
    coordinates.

    The input and output is symmetric to the r2p() function. RADII is
    the magnitude while ANGLES is the angles in radians (default for
    np.angle()).

    NOTE: This function will revert previous normalization as the range of
    values of RADII and ANGLES are mathematically important for the conversion.

    Example using r2p for a regular-polar-regular conversion:
    > polar = r2p(2d_ndarray_containing_iq)
    > polar[0].shape
    (262, 2629)
    > polar[1].shape
    (262, 2629)
    > regular = p2r(polar[0], polar[1])
    > regular.shape
    (262, 2629)
    > np.array_equal(arr, regular)
    False
    > np.isclose(arr, regular)
    array([[ True,  True,  True, ...,  True,  True,  True], ..., [ True,  True,  True, ...,  True,  True,  True]])

    Source: https://stackoverflow.com/questions/16444719/python-numpy-complex-numbers-is-there-a-function-for-polar-to-rectangular-co?rq=4

    """
    if not is_p2r_ready(radii, angles):
        radii  = analyze.normalize(radii,  method=analyze.NormMethod.COMPLEX_ABS)
        angles = analyze.normalize(angles, method=analyze.NormMethod.COMPLEX_ANGLE)
    return radii * np.exp(1j * angles)

def r2p(x):
    """Complex regular to polar.

    Convert a complex number from Regular (Cartesian) coordinates to Polar
    coordinates.

    The input X can be a 1) single complex number 2) a 1D ndarray of complex
    numbers 3) a 2D ndarray of complex numbers. The returned output is a tuple
    composed of a 1) two scalars (float32) representing magnitude and phase 2)
    two ndarray containing the scalars.

    Example using a 2D ndarray as input:
    r2p(arr)[0][1][0] -> magnitude of 1st IQ of 2nd trace.2
    r2p(arr)[1][0][1] -> phase of 2nd IQ of 1st trace.

    Source: https://stackoverflow.com/questions/16444719/python-numpy-complex-numbers-is-there-a-function-for-polar-to-rectangular-co?rq=4
    """
    # abs   = [ 0   ; +inf ] ; sqrt(a^2 + b^2)
    # angle = [ -PI ; +PI  ] ; angle in rad
    return np.abs(x), np.angle(x)
