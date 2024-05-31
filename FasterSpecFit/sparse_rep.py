#
# Sparse representations of resolution matrices and
# Jacobian of objective for emission line fitting
# (Ideal Jacobian rep is generated in EMLines_jacobian())
#

import numpy as np
import scipy.sparse as sp

from numba import jit

from .params_mapping import ParamsMapping

#
# resolution matrix
# A resolution matrix M of size nrow x nrow is stored as a 2D array A of
# size nrow x ndiag, where ndiag is the number of nonzero diagonals
# (which must be odd).  The rows of A are M's rows, but with only the
# nonzero entries stored.  The nonzero entries on row i run from
# j = i - diag//2 to i + diag//2, so
#            M[i,j] = A[i, j - (i - diag//2)]
#
class ResMatrix(object):

    def __init__(self, D):
        self.data = self._from_dia_matrix(D)

    def matvec(self, v, w):
        self._matvec(self.data, v, w)
        
    #
    # _from_dia_matrix()
    # Convert a diagonally sparse matrix M in the form
    # stored by DESI into a sparse row rerpesentation.
    #
    # Input M is represented as a 2D array D of size ndiag x nrow,
    # whose rows are M's diagonals:
    #            M[i,j] = D[ndiag//2 - (j - i), j]
    # ndiag is assumed to be odd, and entries in D that would be
    # outside the bounds of M are ignored.
    #
    @staticmethod
    @jit(nopython=True, fastmath=True, nogil=True)
    def _from_dia_matrix(D):

        ndiag, nrow = D.shape
        hdiag = ndiag//2

        A = np.empty((nrow, ndiag), dtype=D.dtype)
    
        for i in range(nrow):
            # min and max column for row
            jmin = np.maximum(i - hdiag,        0)
            jmax = np.minimum(i + hdiag, nrow - 1)

            for j in range(jmin, jmax + 1):
                A[i, j - i + hdiag] = D[hdiag + i - j, j]
                
        return A
    
    #
    # _matvec()
    # Compute the matrix-vector product M * v, where
    # M is a row-sparse matrix with a limited
    # number of diagonals created by
    # dia_to_row_matrix().
    #
    # w is an output parameter
    #
    @staticmethod
    @jit(nopython=True, fastmath=True, nogil=True)
    def _matvec(M, v, w):

        nrow, ndiag = M.shape
        hdiag = ndiag//2
        
        for i in range(nrow):
            jmin = np.maximum(i - hdiag,    0)
            jmax = np.minimum(i + hdiag, nrow - 1)
            
            acc = 0.
            for j in range(jmin, jmax + 1):
                acc += M[i, j - i + hdiag] * v[j]
        
            w[i] = acc


#################################################################

#
# Sparse Jacobian of objective function.  For
# each camera's pixel range, the Jacobian
# is a matrix product
#
#    W * M * J_I * J_S
#
# where
#  J_S is the Jacobian of the parameter expansion
#  J_I is the ideal Jacobian
#  M is the camera's resolution matrix
#  w is a diagonal matrix of per-observation weights
#
# Note that we precompute W*M*J_I for each camera 
# with the external mulWMJ() function.  This product
# has one contiguous run of nonzero entries per column,
# while J_S has either one or two nonzero entries
# per row.
#
class EMLineJacobian(sp.linalg.LinearOperator):
    
    #
    # CONSTRUCTOR ARGS:
    #   shape of Jacobian
    #   array of start and end obs bin indices
    #     for each camera
    #   partial Jacobian jac = (W * M  J_I)
    #     for each camera
    #   parameter expansion Jacobian J_S
    #
    def __init__(self, shape, camerapix, jacs, J_S):
        
        self.camerapix = camerapix
        self.jacs      = jacs
        self.J_S       = J_S

        # get initialization info from one of jacs
        J0 = jacs[0][2]

        # temporary storage for intermediate result
        nParms = J0.shape[0]
        self.vFull = np.empty(nParms, dtype=J0.dtype)
        
        super().__init__(J0.dtype, shape)


    #
    # Compute left matrix-vector product J * v
    # |v| = number of free parameters
    #
    def _matvec(self, v):

        nBins = self.shape[0]
        w = np.empty(nBins, dtype=v.dtype)
        
        # return result in self.vFull
        ParamsMapping._matvec(self.J_S, v.ravel(), self.vFull)
        
        for campix, jac in zip(self.camerapix, self.jacs):
            s = campix[0]
            e = campix[1]

            # write result to w[s:e]
            self._matvec_J(jac, self.vFull, w[s:e])
            
        return w

    #
    # Compute right matrix product product v * J^T
    # |v| = number of observable bins
    #
    def _rmatvec(self, v):

        nFreeParms = self.shape[1]
        w = np.zeros(nFreeParms, dtype=v.dtype)
        
        for campix, jac in zip(self.camerapix, self.jacs):
            s = campix[0]
            e = campix[1]

            # return result in self.vFull
            self._rmatvec_J(jac, v[s:e], self.vFull)

            # add result to w
            ParamsMapping._rmatvec(self.J_S, self.vFull, w)
        
        return w

    #
    # Multiply ideal Jacobian J * v, writing result to w.
    #
    @staticmethod
    @jit(nopython=True, fastmath=True, nogil=True)
    def _matvec_J(J, v, w):
    
        starts, ends, values = J
        nvars = len(starts)
        nbins = len(w)
        
        for j in range(nbins):
            w[j] = 0.
        
        for i in range(nvars):
            vals = values[i]    # column i
            for j in range(ends[i] - starts[i]):
                w[j + starts[i]] += vals[j] * v[i]  
    
    #
    # Multiply ideal Jacobian v * J^T, writing result to w.
    #
    @staticmethod
    @jit(nopython=True, fastmath=True, nogil=True)
    def _rmatvec_J(J, v, w):
    
        starts, ends, values = J
        nvars = len(starts)
    
        for i in range(nvars):
            vals = values[i]   # row i of transpose
            
            acc = 0.
            for j in range(ends[i] - starts[i]):
                acc += vals[j] * v[j + starts[i]]
            w[i] = acc
