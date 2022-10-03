# pylint: disable=too-few-public-methods
"""
Surrogate models, such as Gaussian processes, random forests, etc.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple, Type

import pandas as pd
import torch
from botorch.fit import fit_gpytorch_model
from botorch.models import SingleTaskGP
from botorch.models.transforms.input import Normalize
from botorch.models.transforms.outcome import Standardize
from botorch.optim.fit import fit_gpytorch_torch

from gpytorch.kernels.matern_kernel import MaternKernel
from gpytorch.kernels.scale_kernel import ScaleKernel
from gpytorch.mlls import ExactMarginalLogLikelihood
from gpytorch.priors.torch_priors import GammaPrior
from torch import Tensor

from .utils import to_tensor


class SurrogateModel(ABC):
    """Abstract base class for all surrogate models."""

    # TODO: to support other models than GPs, an interface to botorch's acquisition
    #  functions must be created (e.g. via a dedicated 'predict' method)

    type: str
    SUBCLASSES: Dict[str, Type[SurrogateModel]] = {}

    @abstractmethod
    def posterior(self, candidates: Tensor) -> Tuple[Tensor, Tensor]:
        """
        Evaluates the surrogate model at the given candidate points.

        Parameters
        ----------
        candidates : torch.Tensor
            The candidate points, represented as a tensor of shape (*t, q, d), where
            't' denotes the "t-batch" shape, 'q' denotes the "q-batch" shape, and
            'd' is the input dimension. For more details about batch shapes, see:
            https://botorch.org/docs/batching

        Returns
        -------
        Tuple[Tensor, Tensor]
            The posterior means and posterior covariance matrices of the t-batched
            candidate points.
        """

    @abstractmethod
    def fit(self, train_x: Tensor, train_y: Tensor) -> None:
        """Trains the surrogate model on the provided data."""

    @classmethod
    def __init_subclass__(cls, **kwargs):
        """Registers new subclasses dynamically."""
        super().__init_subclass__(**kwargs)
        cls.SUBCLASSES[cls.type] = cls


class GaussianProcessModel(SurrogateModel):
    """A Gaussian process surrogate model."""

    type = "GP"

    def __init__(self, searchspace: pd.DataFrame):
        self.model: Optional[SingleTaskGP] = None
        # TODO: the surrogate model should work entirely on Tensors (parameter name
        #  agnostic) -> the scaling information should not be provided in form of a
        #  DataFrame
        self.searchspace = searchspace

    def posterior(self, candidates: Tensor) -> Tuple[Tensor, Tensor]:
        """See base class."""
        posterior = self.model.posterior(candidates)
        return posterior.mvn.mean, posterior.mvn.covariance_matrix

    def fit(self, train_x: Tensor, train_y: Tensor) -> None:
        """See base class."""

        # validate input
        if len(train_x) == 0:
            raise ValueError("The training data set must be non-empty.")
        if train_y.shape[1] != 1:
            raise NotImplementedError("The model currently supports only one target.")

        # get the input bounds from the search space
        searchspace = to_tensor(self.searchspace)
        bounds = torch.vstack(
            [torch.min(searchspace, dim=0)[0], torch.max(searchspace, dim=0)[0]]
        )
        # TODO: use target value bounds when explicitly provided

        # define the input and outcome transforms
        input_transform = Normalize(train_x.shape[1], bounds=bounds)
        outcome_transform = Standardize(train_y.shape[1])

        # select priors
        # TODO temporary prior choices adapted from edbo, replace later on
        batch_shape = train_x.shape[:-2]
        mordred = any("MORDRED" in col for col in self.searchspace.columns) or any(
            "RDKIT" in col for col in self.searchspace.columns
        )
        if mordred and train_x.shape[-1] < 50:
            mordred = False

        # low D priors
        if train_x.shape[-1] < 5:
            covar_module = ScaleKernel(
                MaternKernel(
                    nu=2.5,
                    ard_num_dims=train_x.shape[-1],
                    batch_shape=batch_shape,
                    lengthscale_prior=GammaPrior(1.2, 1.1),
                ),
                batch_shape=batch_shape,
                outputscale_prior=GammaPrior(5.0, 0.5),
            )
            # noise_prior = [GammaPrior(1.05, 0.5), 0.1]
        # DFT optimized priors
        elif mordred and train_x.shape[-1] < 100:
            covar_module = ScaleKernel(
                MaternKernel(
                    nu=2.5,
                    ard_num_dims=train_x.shape[-1],
                    batch_shape=batch_shape,
                    lengthscale_prior=GammaPrior(2.0, 0.2),
                ),
                batch_shape=batch_shape,
                outputscale_prior=GammaPrior(5.0, 0.5),
            )
            # noise_prior = [GammaPrior(1.5, 0.1), 5.0]
        # Mordred optimized priors
        elif mordred:
            covar_module = ScaleKernel(
                MaternKernel(
                    nu=2.5,
                    ard_num_dims=train_x.shape[-1],
                    batch_shape=batch_shape,
                    lengthscale_prior=GammaPrior(2.0, 0.1),
                ),
                batch_shape=batch_shape,
                outputscale_prior=GammaPrior(2.0, 0.1),
            )
            # noise_prior = [GammaPrior(1.5, 0.1), 5.0]
        # OHE optimized priors
        else:
            covar_module = ScaleKernel(
                MaternKernel(
                    nu=2.5,
                    ard_num_dims=train_x.shape[-1],
                    batch_shape=batch_shape,
                    lengthscale_prior=GammaPrior(3.0, 1.0),
                ),
                batch_shape=batch_shape,
                outputscale_prior=GammaPrior(5.0, 0.2),
            )
            # noise_prior = [GammaPrior(1.5, 0.1), 5.0]

        # construct and fit the Gaussian process
        self.model = SingleTaskGP(
            train_x,
            train_y,
            input_transform=input_transform,
            outcome_transform=outcome_transform,
            covar_module=covar_module,
        )
        mll = ExactMarginalLogLikelihood(self.model.likelihood, self.model)
        fit_gpytorch_model(mll, optimizer=fit_gpytorch_torch, options={"disp": False})