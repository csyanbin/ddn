#!/usr/bin/env python
#
# OPTIMAL TRANSPORT NODE
# Implementation of differentiable optimal transport using implicit differentiation. Makes use of Sinkhorn normalization
# to solve the entropy regularized problem (Cuturi, NeurIPS 2013) in the forward pass. The problem can be written as
# Let us write the entropy regularized optimal transport problem in the following form,
#
#    minimize (over P) <P, M> + 1/\gamma KL(P || rc^T)
#    subject to        P1 = r and P^T1 = c
#
# where r and c are m- and n-dimensional positive vectors, respectively, each summing to one. Here m-by-n matrix M is
# the input and m-by-n dimensional positive matrix P is the output. The above problem leads to a solution of the form
#
#   P_{ij} = \alpha_i \beta_j e^{-\gamma M_{ij}}
#
# where \alpha and \beta are found by iteratively applying row and column normalizations.
#
# See accompanying Jupyter Notebook at https://deepdeclarativenetworks.com.
#
# Stephen Gould <stephen.gould@anu.edu.au>
# Dylan Campbell <dylan.campbell@anu.edu.au>
#

import torch
import torch.nn as nn


def sinkhorn(M, r=None, c=None, gamma=1.0, eps=1.0e-6, maxiters=1000):
    """
    PyTorch function for entropy regularized optimal transport. Assumes batched inputs as follows:
        M:  (B,H,W) tensor
        r:  (B,H) tensor, (1,H) tensor or None for constant uniform vector 1/H
        c:  (B,W) tensor, (1,W) tensor or None for constant uniform vector 1/W

    You can back propagate through this function in O(TBWH) time where T is the number of iterations taken to converge.
    """

    B, H, W = M.shape
    assert r is None or r.shape == (B, H) or r.shape == (1, H)
    assert c is None or c.shape == (B, W) or c.shape == (1, W)

    if r is None: r = 1.0 / H
    if c is None: c = 1.0 / W

    P = torch.exp(-1.0 * gamma * (M - torch.amin(M, 2, keepdim=True)))
    for i in range(maxiters):
        alpha = torch.sum(P, 2)
        # Perform division first for numerical stability
        P = P / alpha.view(B, H, 1) * r

        beta = torch.sum(P, 1)
        if torch.max(torch.abs(beta - c)) <= eps:
            break
        P = P / beta.view(B, 1, W) * c

    return P


def _sinkhorn_inline(M, r=None, c=None, gamma=1.0, eps=1.0e-6, maxiters=1000):
    """As above but with inline calculations for when autograd is not needed."""

    B, H, W = M.shape
    assert r is None or r.shape == (B, H) or r.shape == (1, H)
    assert c is None or c.shape == (B, W) or c.shape == (1, W)

    if r is None: r = 1.0 / H
    if c is None: c = 1.0 / W

    P = torch.exp(-1.0 * gamma * (M - torch.amin(M, 2, keepdim=True)))
    for i in range(maxiters):
        alpha = torch.sum(P, 2)
        # Perform division first for numerical stability
        P /= alpha.view(B, H, 1); P *= r

        beta = torch.sum(P, 1)
        if torch.max(torch.abs(beta - c)) <= eps:
            break
        P /= beta.view(B, 1, W); P *= c

    return P


class OptimalTransportFcn(torch.autograd.Function):
    """
    PyTorch autograd function for entropy regularized optimal transport. Assumes batched inputs as follows:
        M:  (B,H,W) tensor
        r:  (B,H) tensor, (1,H) tensor or None for constant uniform vector
        c:  (B,W) tensor, (1,W) tensor or None for constant uniform vector

    Allows for approximate gradient calculations, which is faster and may be useful during early stages of learning,
    when exp(-\gamma M) is already nearly doubly stochastic, or when gradients are otherwise noisy.

    Both r and c must be positive, if provided. They will be normalized to sum to one.
    """

    @staticmethod
    def forward(ctx, M, r=None, c=None, gamma=1.0, eps=1.0e-6, maxiters=1000, approx_grad=False, block_inverse=True):
        """Solve optimal transport using skinhorn."""

        with torch.no_grad():
            # normalize r and c to ensure that they sum to one (and save normalization factor for backward pass)
            if r is not None:
                ctx.inv_r_sum = 1.0 / torch.sum(r, dim=1, keepdim=True)
                r = ctx.inv_r_sum * r
            if c is not None:
                ctx.inv_c_sum = 1.0 / torch.sum(c, dim=1, keepdim=True)
                c = ctx.inv_c_sum * c

            # run sinkhorn
            P = _sinkhorn_inline(M, r, c, gamma, eps, maxiters)

        ctx.save_for_backward(M, r, c, P)
        ctx.gamma = gamma
        ctx.approx_grad = approx_grad
        ctx.block_inverse = block_inverse

        return P

    @staticmethod
    def backward(ctx, dJdP):
        """Implement backward pass using implicit differentiation."""

        M, r, c, P = ctx.saved_tensors
        B, H, W = M.shape

        # initialize backward gradients (-v^T H^{-1} B with v = dJdP and B = I or B = -1/r or B = -1/c)
        dJdM = -1.0 * ctx.gamma * P * dJdP
        dJdr = None if not ctx.needs_input_grad[1] else torch.zeros_like(r)
        dJdc = None if not ctx.needs_input_grad[2] else torch.zeros_like(c)

        # return approximate gradients
        if ctx.approx_grad:
            return dJdM, dJdr, dJdc, None, None, None, None, None

        # compute exact row and column sums (in case of small numerical errors)
        row_sum = P.sum(2)
        col_sum = P.sum(1)

        # compute [vHAt1, vHAt2] = v^T H^{-1} A^T as two blocks
        vHAt1 = torch.sum(dJdM[:, 1:H, 0:W], dim=2)
        vHAt2 = torch.sum(dJdM, dim=1)

        # compute [v1, v2] = -v^T H^{-1} A^T (A H^{-1] A^T)^{-1}
        if ctx.block_inverse:
            # by block inverse of (A H^{-1] A^T)
            PdivC = P[:, 1:H, 0:W] / col_sum.view(B, 1, W)
            block_11 = torch.cholesky(torch.diag_embed(row_sum[:, 1:H]) - torch.einsum("bij,bkj->bik", P[:, 1:H, 0:W], PdivC))
            block_12 = torch.cholesky_solve(PdivC, block_11)
            block_22 = torch.diag_embed(1.0 / col_sum) + torch.einsum("bji,bjk->bik", block_12, PdivC)

            v1 = torch.cholesky_solve(vHAt1.view(B, H-1, 1), block_11).view(B, H-1) - torch.einsum("bi,bji->bj", vHAt2, block_12)
            v2 = torch.einsum("bi,bij->bj", vHAt2, block_22) - torch.einsum("bi,bij->bj", vHAt1, block_12)

        else:
            # by full inverse of (A H^{-1] A^T)
            AinvHAt = torch.empty((B, H + W - 1, H + W - 1), device=M.device, dtype=M.dtype)
            AinvHAt[:, 0:H - 1, 0:H - 1] = torch.diag_embed(row_sum[:, 1:H])
            AinvHAt[:, H - 1:H + W - 1, H - 1:H + W - 1] = torch.diag_embed(col_sum)
            AinvHAt[:, 0:H - 1, H - 1:H + W - 1] = P[:, 1:H, 0:W]
            AinvHAt[:, H - 1:H + W - 1, 0:H - 1] = P[:, 1:H, 0:W].transpose(1, 2)

            v = torch.einsum("bi,bij->bj", torch.cat((vHAt1, vHAt2), dim=1), torch.inverse(AinvHAt))
            v1 = v[:, 0:H-1]
            v2 = v[:, H-1:H+W-1]

        # compute v^T H^{-1} A^T (A H^{-1] A^T)^{-1} A H^{-1} B - v^T H^{-1} B
        dJdM[:, 1:H, 0:W] -= v1.view(B, H-1, 1) * P[:, 1:H, 0:W]
        dJdM -= v2.view(B, 1, W) * P

        # compute v^T H^{-1} A^T (A H^{-1] A^T)^{-1} (A H^{-1} B - C) - v^T H^{-1} B
        if dJdr is not None:
            dJdr = ctx.inv_r_sum.view(r.shape[0], 1) / ctx.gamma * \
                   (torch.sum(r[:, 1:H] * v1, dim=1, keepdim=True) - torch.cat((torch.zeros(B, 1), v1), dim=1))

        # compute v^T H^{-1} A^T (A H^{-1] A^T)^{-1} (A H^{-1} B - C) - v^T H^{-1} B
        if dJdc is not None:
            dJdc = ctx.inv_c_sum.view(c.shape[0], 1) / ctx.gamma * (torch.sum(c * v2, dim=1, keepdim=True) - v2)

        # return gradients (None for gamma, eps, and maxiters)
        return dJdM, dJdr, dJdc, None, None, None, None, None


class OptimalTransportLayer(nn.Module):
    """
    Neural network layer to implement optimal transport.

    Parameters:
    -----------
    gamma: float, default: 1.0
        Inverse of the coeffient on the entropy regularisation term.
    eps: float, default: 1.0e-6
        Tolerance used to determine the stop condition.
    maxiters: int, default: 1000
        The maximum number of iterations.
    approx_grad: bool, default: False
        If `True`, approximate the gradient by assuming exp(-\gamma M) is already nearly doubly stochastic.
        It is faster and could potentially be useful during early stages of training.
    block_inverse: bool, default: True
        If `True`, exploit the block structure of matrix A H^{-1] A^T. 
    """

    def __init__(self, gamma=1.0, eps=1.0e-6, maxiters=1000, approx_grad=False, block_inverse=True):
        super(OptimalTransportLayer, self).__init__()
        self.gamma = gamma
        self.eps = eps
        self.maxiters = maxiters
        self.approx_grad = approx_grad
        self.block_inverse = block_inverse

    def forward(self, M, r=None, c=None):
        """
        Parameters:
        -----------
        M: torch.Tensor
            Input matrix/matrices of size (H, W) or (B, H, W)
        r: torch.Tensor, optional
            Row sum constraint in the form of a 1xH or BxH matrix. Are assigned uniformly as 1/H by default.
        c: torch.Tensor, optional
            Column sum constraint in the form of a 1xW or BxW matrix. Are assigned uniformly as 1/W by default.

        Returns:
        --------
        torch.Tensor
            Normalised matrix/matrices, with the same shape as the inputs
        """
        M_shape = M.shape
        # Check the number of dimensions
        ndim = len(M_shape)
        if ndim == 2:
            M = M.unsqueeze(dim=0)
        elif ndim != 3:
            raise ValueError(f"The shape of the input tensor {M_shape} does not match that of an matrix")
        
        # Handle special case of 1x1 matrices
        nr, nc = M_shape[-2:]
        if nr == 1 and nc == 1:
            P = M / M
        else:
            P = OptimalTransportFcn.apply(M, r, c, self.gamma, self.eps, self.maxiters, self.approx_grad, self.block_inverse)

        return P.view(*M_shape)


#
# --- testing ---
#

if __name__ == '__main__':

    from torch.autograd import gradcheck
    from torch.nn.functional import normalize

    torch.manual_seed(0)

    M = torch.randn((3, 5, 7), dtype=torch.double, requires_grad=True)
    f = OptimalTransportFcn().apply
    test = gradcheck(f, (M, None, None, 1.0, 1.0e-6, 1000, False, True), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    test = gradcheck(f, (M, None, None, 1.0, 1.0e-6, 1000, False, False), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    test = gradcheck(f, (M, None, None, 10.0, 1.0e-6, 1000, False, True), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    test = gradcheck(f, (M, None, None, 10.0, 1.0e-6, 1000, False, False), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    r = normalize(torch.rand((M.shape[0], M.shape[1]), dtype=torch.double, requires_grad=False), p=1.0)
    c = normalize(torch.rand((M.shape[0], M.shape[2]), dtype=torch.double, requires_grad=False), p=1.0)

    test = gradcheck(f, (M, r, c, 1.0, 1.0e-9, 1000, False, True), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    # with r and c inputs
    r = normalize(torch.rand((M.shape[0], M.shape[1]), dtype=torch.double, requires_grad=True), p=1.0)
    c = normalize(torch.rand((M.shape[0], M.shape[2]), dtype=torch.double, requires_grad=True), p=1.0)

    test = gradcheck(f, (M, r, None, 1.0, 1.0e-6, 1000, False, True), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    test = gradcheck(f, (M, None, c, 1.0, 1.0e-6, 1000, False, True), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    test = gradcheck(f, (M, r, c, 1.0, 1.0e-6, 1000, False, True), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    test = gradcheck(f, (M, r, c, 10.0, 1.0e-6, 1000, False, True), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    # shared r and c
    r = normalize(torch.rand((1, M.shape[1]), dtype=torch.double, requires_grad=True), p=1.0)
    c = normalize(torch.rand((1, M.shape[2]), dtype=torch.double, requires_grad=True), p=1.0)

    test = gradcheck(f, (M, r, c, 1.0, 1.0e-6, 1000, False, True), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)

    test = gradcheck(f, (M, r, c, 1.0, 1.0e-6, 1000, False, False), eps=1e-6, atol=1e-3, rtol=1e-6)
    print(test)
