#
# Multicore implementation of spectral fitting via Gaussian
# integration.
#

import numpy as np
from math import erf, erfc

import scipy.sparse as sp
from scipy.optimize import least_squares

from numba import jit

from .sparse_rep import EMLineSparseArray

# Do not bother computing normal PDF/CDF if more than this many 
# standard deviations from mean.
MAX_SDEV = 5.

#
# norm_pdf()
# PDF of standard normal distribution at a point a
#
@jit(nopython=True, fastmath=True, nogil=True)
def norm_pdf(a):

    SQRT_2PI = np.sqrt(2 * np.pi)
    
    return 1/SQRT_2PI * np.exp(-0.5 * a**2)


#
# norm_cdf()
# Approximate the integral of a standard normal PDF from -infty to a.
#
# Optimization (currently disabled because it is not needed): If
# |a| > MAX_SDEV, treat the value as extreme and return 0 or 1 as
# appropriate.
#
@jit(nopython=True, fastmath=True, nogil=True)
def norm_cdf(a):

    SQRT1_2 = 1.0 / np.sqrt(2)
    
    z = np.abs(a)

    #if z > MAX_SDEV: # short-circuit extreme values
    #    if a > 0:
    #        y = 1.
    #    else:
    #        y = 0.
    if z < 1.:
        y = 0.5 + 0.5 * erf(a * SQRT1_2)
    else:
        y = 0.5 * erfc(z * SQRT1_2)
        if a > 0:
            y = 1.0 - y
    
    return y


#
# build_emline_model() 
#
# Given a fixed set of spectral lines and known redshift, and estimates
# for the amplitude of each line and a common velocity shift and line width,
# compute the average value observed in each of a series of spectral bins
# whose edges are defined by obs_bin_edges.
#
# INPUTS:
#   line_amplitudes -- amplitudes for each fitted spectral line
#   line_vshifts   -- additional velocity shift for each fitted lines
#   line_sigmas    -- width of Gaussian profile for each fitted lines
#
#   obs_bin_edges     -- edges for observed spectral bins in ascending order
#   log_obs_bin-edges -- natural log of values in obs_bin_edges
#   redshift          -- red shift of observed spectrum
#   line_wavelengths  -- array of wavelengths for all fitted spectral lines
#
# RETURNS:
#   vector of average fluxes in each observed wavelength bin
#
@jit(nopython=True, fastmath=True, nogil=True)
def build_emline_model(parameters,
                       obs_bin_edges,
                       log_obs_bin_edges,
                       redshift,
                       line_wavelengths):
    
    C_LIGHT = 299792.458
    SQRT_2PI = np.sqrt(2 * np.pi)

    line_amplitudes, line_vshifts, line_sigmas = \
        np.array_split(parameters, 3)

    ibin_width = np.hstack((np.array([0.]), 1/np.diff(obs_bin_edges)))
    
    nbins = len(obs_bin_edges) - 1
    
    # output per-bin fluxes
    # entry i corresponds bin i-1
    # entry 0 is a dummy in case lo == 0
    # last entry is a dummy in case hi == len(obs_bin_edges)
    model_fluxes = np.zeros(nbins + 2)

    # temporary buffer for per-line calculations, sized large
    # enough for whatever we may need to compute ([lo - 1 .. hi])
    max_width = int(2*MAX_SDEV*np.max(line_sigmas/C_LIGHT) / \
                    np.min(np.diff(log_obs_bin_edges))) + 4
    edge_vals = np.empty(max_width)  

    # compute total area of all Gaussians inside each bin.
    # For each Gaussian, we only compute area contributions
    # for bins where it is non-negligible.
    for j in range(len(line_wavelengths)):

        # line width
        sigma = line_sigmas[j] / C_LIGHT
        c = SQRT_2PI * sigma * np.exp(0.5 * sigma**2)
    
        # wavelength shift for spectral lines
        line_shift = 1. + redshift + line_vshifts[j] / C_LIGHT
    
        shifted_line     = line_wavelengths[j] * line_shift
        log_shifted_line = np.log(shifted_line)
        
        # leftmost edge i that needs a value (> 0) for this line 
        lo = np.searchsorted(log_obs_bin_edges,
                             log_shifted_line - MAX_SDEV * sigma,
                             side="left")
        
        # leftmost edge i that does *not* need a value (== 1) for this line
        hi = np.searchsorted(log_obs_bin_edges,
                             log_shifted_line + MAX_SDEV * sigma,
                             side="right")
        
        if hi == lo:  # entire Gaussian is outside bounds of obs_bin_edges
            continue
        
        nedges = hi - lo + 2  # compute values at edges [lo - 1 ... hi]
        
        A = c * line_amplitudes[j] * shifted_line
        offset = log_shifted_line / sigma + sigma
        
        # vals[i] --> edge i + lo - 1
        
        edge_vals[0] = 0. # edge lo - 1
        
        for i in range(1, nedges-1):
            
            # x = (log(lambda_j) - mu_i)/sigma - sigma,
            # the argument of the Gaussian integral
            
            x = log_obs_bin_edges[i+lo-1]/sigma - offset
            edge_vals[i] = A * norm_cdf(x)
        
        edge_vals[nedges-1] = A  # edge hi
        
        # convert vals[i] to avg of bin i+lo-1 (last value is garbage)
        # we get values for bins lo-1 to hi-1 inclusive
        for i in range(nedges-1):
            edge_vals[i] = (edge_vals[i+1] - edge_vals[i]) * ibin_width[i+lo]
        
        # add bin avgs for this peak to the full array
        model_fluxes[lo:hi+1] += edge_vals[:nedges-1] 
        
    # trim off left and right dummy values before returning
    return model_fluxes[1:-1]



#
# Objective function for least-squares optimization Build the emline
# model as described above and compute the weighted vector of
# residuals between the modeled fluxes and the observations.
#
# *args contains the fixed arguments to build_emline_model()
#
#@jit(nopython=True, fastmath=False, nogil=True)
def _objective(free_parameters,
               obs_bin_edges,
               log_obs_bin_edges,
               obs_fluxes,
               obs_weights,
               redshift,
               line_wavelengths,
               resolution_matrix,
               camerapix,
               parameters,
               Ifree,
               Itied,
               tiedtoparam,
               tiedfactor,
               doubletindx,
               doubletpair):

    #
    # expand free paramters into complete
    # parameter array, handling tied params
    # and doublets
    #
    
    parameters[Ifree] = free_parameters
    if len(Itied) > 0:
        for I, indx, factor in zip(Itied, tiedtoparam, tiedfactor):
            parameters[I] = parameters[indx] * factor
    
    # fragile! assumes the amplitudes are always first...
    parameters[doubletindx] *= parameters[doubletpair]

    model_fluxes = np.empty_like(obs_fluxes)
    
    for icam, campix in enumerate(camerapix):

        # start and end for obs fluxes of camera icam
        s = campix[0]
        e = campix[1]
        
        mf = build_emline_model(parameters,
                                obs_bin_edges[s+icam:e+icam+1],
                                log_obs_bin_edges[s+icam:e+icam+1],
                                redshift,
                                line_wavelengths)
        
        model_fluxes[s:e] = resolution_matrix[icam].dot(mf)
    
    residuals = obs_weights * (obs_fluxes - model_fluxes) # data minus model
    
    return residuals


"""
# replacement for np.tile, which is not supported by Numba
@jit(nopython=True, fastmath=True, nogil=True)
def mytile(a, n):
    sz = len(a)
    r = np.empty(n * sz, dtype=a.dtype)
    for i in range(n):
        r[i*sz:(i+1)*sz] = a
    return r


#
# build_emline_model_jacobian() 
#
# Compute the Jacobian of the function computed in build_emlines_model().
# Inputs are as for build_emlines_model(), except for
#
#  obs_weights -- weights for observations in each bin
#
# (passed here so that we can apply them sparsely).
#
# RETURNS:
# Jacobian as a *dense* matrix (Numba does not support scipy.sparse)
#
@jit(nopython=True, fastmath=True, nogil=True)
def build_emline_model_jacobian(parameters,
                                obs_weights,
                                obs_bin_edges,
                                log_obs_bin_edges,
                                resolution_matrix,
                                redshift,
                                line_wavelengths):
    
    C_LIGHT = 299792.458
    SQRT_2PI = np.sqrt(2*np.pi)
    
    line_amplitudes, line_vshifts, line_sigmas = \
        np.array_split(parameters, 3)
        
    w = np.hstack((np.array([0.]), obs_weights / np.diff(obs_bin_edges)))
    
    max_width = int(2*MAX_SDEV*np.max(line_sigmas/C_LIGHT) / \
                    np.min(np.diff(log_obs_bin_edges))) + 4
    
    nlines = len(line_wavelengths)
    dd     = np.empty((3 * nlines, max_width))
    starts = np.zeros(nlines, dtype=np.int32)
    ends   = np.zeros(nlines, dtype=np.int32)
    
    # compute partial derivatives for avg values of all Gaussians
    # inside each bin. For each Gaussian, we only compute
    # contributions for bins where it is non-negligible.
    for j in range(len(line_wavelengths)):
        
        # line width
        sigma = line_sigmas[j] / C_LIGHT
        c0 = SQRT_2PI * np.exp(0.5 * sigma**2)
        
        # wavelength shift for spectral lines
        line_shift = 1. + redshift + line_vshifts[j] / C_LIGHT
        shifted_line     = line_wavelengths[j] * line_shift
        log_shifted_line = np.log(shifted_line)
        
        # leftmost edge i that needs a value (> 0) for this line 
        lo = np.searchsorted(log_obs_bin_edges,
                             log_shifted_line - MAX_SDEV * sigma,
                             side="left")
        
        # leftmost edge i that does *not* need a value (== 1) for this line
        hi = np.searchsorted(log_obs_bin_edges,
                             log_shifted_line + MAX_SDEV * sigma,
                             side="right")
        
        if hi == lo:  # Gaussian is entirely outside bounds of obs_bin_edges
            continue
        
        nedges = hi - lo + 2 # compute values at edges [lo - 1 ... hi]
        
        # Compute contribs of each line to each partial derivative in place.
        # No sharing of params between peaks means that we never have to
        # add contributions from two peaks to same line.
        dda_vals = dd[           j]
        ddv_vals = dd[nlines   + j]
        dds_vals = dd[2*nlines + j]
        
        offset = log_shifted_line / sigma + sigma
        
        c = c0 * line_wavelengths[j]
        A = c / C_LIGHT * line_amplitudes[j]
        
        # vals[i] --> edge i + lo - 1
        
        dda_vals[0] = 0. # edge lo - 1
        ddv_vals[0] = 0.
        dds_vals[0] = 0.
        
        for i in range(1, nedges - 1):
            
            # x - offset = (log(lambda_j) - mu_i)/sigma - sigma,
            # the argument of the Gaussian integral
            
            x = log_obs_bin_edges[i+lo-1]/sigma - offset
            pdf = norm_pdf(x)
            cdf = norm_cdf(x)
            
            dda_vals[i] = c * line_shift * sigma * cdf
            ddv_vals[i] = A * (sigma * cdf - pdf)
            dds_vals[i] = A * line_shift * \
                ((1 + sigma**2) * cdf - (x + 2*sigma) * pdf)
            
        dda_vals[nedges - 1] = c * line_shift * sigma     # edge hi
        ddv_vals[nedges - 1] = A * sigma
        dds_vals[nedges - 1] = A * line_shift * (1 + sigma**2)
        
        # convert *_vals[i] to partial derivatives for bin i+lo-1 (last value
        # in each array is garbage)
        # we get values for bins lo-1 to hi-1 inclusive
        for i in range(nedges - 1):
            dda_vals[i] = (dda_vals[i+1] - dda_vals[i]) * w[i+lo]
            ddv_vals[i] = (ddv_vals[i+1] - ddv_vals[i]) * w[i+lo]
            dds_vals[i] = (dds_vals[i+1] - dds_vals[i]) * w[i+lo]

        # don't need to fix the garbage, as sparse rep does not use it
        #dda_vals[nedges - 1] = 0. # actual derivative for this bin
        #ddv_vals[nedges - 1] = 0. # actual derivative for this bin
        #dds_vals[nedges - 1] = 0. # actual derivative for this bin
        
        if lo == 0:
            starts[j] = lo
            
            dda_vals[0:nedges - 1] = dda_vals[1:nedges]
            ddv_vals[0:nedges - 1] = ddv_vals[1:nedges]
            dds_vals[0:nedges - 1] = dds_vals[1:nedges]
            # 0 entry of dd*_vals is dummy
        else:
            starts[j] = lo - 1
            # 0 entry is valid
        
        # one past last valid entry
        ends[j] = hi
    
    # missing step -- apply the resolution matrix
    print('ToDo: apply the resolution matrix here!')
    
    return (mytile(starts, 3), mytile(ends, 3), dd)
    

#
# Jacobian of objective bjective function for least-squares
# optimization. The result of the detailed calculation is converted
# to a sparse matrix, since it is extremely sparse, to speed up
# subsequent matrix-vector multiplies in the optimizer.
#
# *args contains the fixed arguments to build_emline_model()
# which are all used in the Jacobian calculation.
#
def _jacobian(parameters,
              obs_fluxes,  # not used
              obs_weights,
              *args):

    starts, ends, dd = \
        build_emline_model_jacobian(parameters,
                                    obs_weights,
                                    *args)
    
    return EMLineSparseArray((len(obs_fluxes), len(parameters)),
                             starts, ends, dd)

"""


#
# centes_to_edges()
# Convert N bin centers to N+1 bin edges.  Edges are placed
# halfway between centers, with extrapolation at the ends.
#
def centers_to_edges(centers, camerapix):

    ncameras = camerapix.shape[0]
    edges = np.empty(len(centers) + ncameras, dtype=centers.dtype)
    
    for icam, campix in enumerate(camerapix):

        s = campix[0]
        e = campix[1]
        icenters = centers[s:e]
        
        #- interior edges are just points half way between bin centers
        int_edges = 0.5 * (icenters[:-1] + icenters[1:])
        
        #- exterior edges are extrapolation of interior bin sizes
        edge_l = icenters[ 0] - (icenters[ 1] - int_edges[ 0])
        edge_r = icenters[-1] + (icenters[-1] - int_edges[-1])

        edges[s+icam:e+icam+1] = np.hstack((edge_l, int_edges, edge_r))

    return edges

"""
#
# emlines()
# Fit a set of noisy flux measurements to an underlying collection of
# spectral lines, assigning an amplitude to each line.
#
# INPUTS:
# obs_wavelengths: vector of wavelengths at which flux was measured
# obs_fluxes:      fluxes at each observed wavelength
# obs_ivar:        1/variance of flux of each observed wavelength
#
# redshift:       redshift of object's spectrum
# line_wavelengths: wavelengths of spectral lines being fit to data
#
# RETURNS:
# fitted line amplitudes, fitted velocity shift, fitted width,
# objective value
#
def emlines(obs_wavelengths,
            obs_fluxes,
            obs_ivar,
            redshift,
            line_wavelengths):
    
    # statistical weights of observed fluxes
    obs_weights = np.sqrt(obs_ivar)
    obs_bin_edges = centers_to_edges(obs_wavelengths)
    farg = (
        obs_fluxes,
        obs_weights,
        obs_bin_edges,
        np.log(obs_bin_edges),
        redshift,
        line_wavelengths,
    )
    
    # initial guesses for parameters
    init_amplitudes = np.full_like(line_wavelengths, 1.)
    init_vshifts    = np.full_like(line_wavelengths, 0.)
    init_sigmas     = np.full_like(line_wavelengths, 75.)
        
    init_vals = np.hstack((init_amplitudes, init_vshifts, init_sigmas))
    
    # lower and upper bounds for params
    # lower and upper bounds for params
    n = len(line_wavelengths)
    bounds_min =  [0.]   * n + [-100.] * n + [  0.]  * n
    bounds_max =  [1e+3] * n + [+100.] * n + [500.]  * n
    
    # optimize!
    fit_info = least_squares(_objective,
                             init_vals,
                             jac=_jacobian,
                             bounds=[bounds_min, bounds_max],
                             args=farg,
                             max_nfev=5000,
                             xtol=1e-10,
                             method="trf",
                             #verbose=2,
                             # x_scale="jac",
                             tr_solver="lsmr",
                             tr_options={"regularize": True})

    # extract solution
    params = fit_info.x
    fitted_amplitudes, fitted_vshifts, fitted_sigmas = \
        np.array_split(params, 3)
    objval = fit_info.cost
    
    return fitted_amplitudes, fitted_vshifts, fitted_sigmas, objval
"""