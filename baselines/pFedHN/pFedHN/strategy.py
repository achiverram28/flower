"""Strategy of the Federated Learning."""

from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple, Union

import flwr as fl
import torch
from flwr.common import (
    EvaluateIns,
    EvaluateRes,
    FitIns,
    FitRes,
    NDArrays,
    Parameters,
    Scalar,
    ndarrays_to_parameters,
    parameters_to_ndarrays,
)
from flwr.server.client_manager import ClientManager
from flwr.server.client_proxy import ClientProxy

from pFedHN.models import CNNHyper


# pylint: disable=invalid-name
# pylint: disable=too-many-instance-attributes
class pFedHN(fl.server.strategy.Strategy):
    """Federated strategy with pFedHN."""

    # pylint: disable=too-many-arguments
    def __init__(
        self,
        config,
        fraction_fit: float = 0.0,
        fraction_evaluate: float = 1.0,
        min_fit_clients: int = 1,
        min_evaluate_clients: int = 1,
        min_available_clients: int = 1,
        accept_failures: bool = True,
        initial_parameters: Optional[Parameters] = None,
        evaluate_fn: Optional[
            Callable[
                [int, NDArrays, Dict[str, Scalar]],
                Optional[Tuple[float, Dict[str, Scalar]]],
            ]
        ] = None,
    ) -> None:
        super().__init__()
        self.fraction_fit = fraction_fit
        self.fraction_evaluate = fraction_evaluate
        self.min_fit_clients = min_fit_clients
        self.min_evaluate_clients = min_evaluate_clients
        self.min_available_clients = min_available_clients
        self.accept_failures = accept_failures
        self.initial_parameters = initial_parameters
        self.cfg = config
        self.evaluate_fn = evaluate_fn
        # Initialising our hnet model in server
        embedding_dim_comp = self.cfg.client.num_nodes_stratergy_client
        self.hnet = CNNHyper(
            n_nodes=self.cfg.client.num_nodes,
            embedding_dim=embedding_dim_comp,
            in_channels=self.cfg.model.in_channels,
            n_kernels=self.cfg.model.n_kernels,
            out_dim=self.cfg.model.out_dim,
            hidden_dim=100,
            n_hidden=1,
        )

    def __repr__(self) -> str:
        """Return the strategy name."""
        return "pFedHN"

    def initialize_parameters(
        self, client_manager: ClientManager
    ) -> Optional[Parameters]:
        """Initialize global model parameters."""
        client_manager.num_available()

        self.hnet.to(torch.device("cpu"))

        initial_parameters = self.initial_parameters
        self.initial_parameters = None
        return initial_parameters

    def num_fit_clients(self, num_available_clients: int) -> Tuple[int, int]:
        """Return sample size and required number of clients."""
        return min(1, num_available_clients), self.min_available_clients

    def num_evaluate_clients(self, num_available_clients: int) -> Tuple[int, int]:
        """Use a fraction of available clients for evaluation."""
        return min(0, num_available_clients), self.min_available_clients

    def configure_fit(
        self, server_round: int, parameters: Parameters, client_manager: ClientManager
    ) -> List[Tuple[ClientProxy, FitIns]]:
        """Configure the next round of training."""
        sample_size, min_num_clients = self.num_fit_clients(
            client_manager.num_available()
        )

        clients = client_manager.sample(
            num_clients=sample_size,
            min_num_clients=min_num_clients,
        )

        self.hnet.train()

        # function to generate weights for each client
        def weights_to_clients(client_id):
            weights = self.hnet(
                torch.tensor([client_id], dtype=torch.long).to(torch.device("cpu"))
            )
            return weights

        fit_configurations = []
        for _idx, client in enumerate(clients):
            # pylint: disable=attribute-defined-outside-init
            self.weights = weights_to_clients(int(client.cid))
            array = [val.cpu().detach().numpy() for _, val in self.weights.items()]

            # Converting the weights to parameters
            parameters = ndarrays_to_parameters(array)
            fit_configurations.append((client, FitIns(parameters, {})))

        return fit_configurations

    def gradient_upgradation(self, delta_theta_param: Parameters):
        """Update the gradients of the hypernetwork."""
        optim = torch.optim.Adam(params=self.hnet.parameters(), lr=1e-2)
        optim.zero_grad()

        param_dict = zip(
            self.hnet.state_dict().keys(),
            fl.common.parameters_to_ndarrays(delta_theta_param),
        )
        delta_theta = OrderedDict({k: torch.Tensor(v) for k, v in param_dict})

        weights = self.weights

        # calculating phi gradient
        hnet_grads = torch.autograd.grad(
            list(weights.values()),
            self.hnet.parameters(),
            grad_outputs=list(delta_theta.values()),
            allow_unused=True,
        )

        # update hnet weights
        for p, g in zip(self.hnet.parameters(), hnet_grads):
            p.grad = g

        torch.nn.utils.clip_grad_norm_(self.hnet.parameters(), 50)
        optim.step()

    def aggregate_fit(
        self,
        server_round: int,
        results: List[Tuple[ClientProxy, FitRes]],
        failures: List[Union[Tuple[ClientProxy, FitRes], BaseException]],
    ) -> Tuple[Optional[Parameters], Dict[str, Scalar]]:
        """Use the delta_theta to update the hypernetwork."""
        _, fit_res = results[0]

        delta_theta = fit_res.parameters

        self.gradient_upgradation(delta_theta)

        return None, {}

    def configure_evaluate(
        self, server_round: int, parameters: Parameters, client_manager: ClientManager
    ) -> List[Tuple[ClientProxy, EvaluateIns]]:
        """Configure the next round of evaluation."""
        sample_size, min_num_clients = self.num_evaluate_clients(
            client_manager.num_available()
        )
        clients = client_manager.sample(
            num_clients=sample_size, min_num_clients=min_num_clients
        )
        eval_configurations = []
        for _idx, client in enumerate(clients):
            eval_configurations.append(
                (client, EvaluateIns(parameters, {"server_round": server_round}))
            )

        return eval_configurations

    def aggregate_evaluate(
        self,
        server_round: int,
        results: List[Tuple[ClientProxy, EvaluateRes]],
        failures: List[Union[Tuple[ClientProxy, EvaluateRes], BaseException]],
    ) -> Tuple[Optional[float], Dict[str, Scalar]]:
        """Aggregate evaluation metrics."""
        if not results:
            return None, {}
        return None, {}

    def evaluate(
        self, server_round: int, parameters: Parameters
    ) -> Optional[Tuple[float, Dict[str, Scalar]]]:
        """Evaluate model parameters using an evaluation function."""
        if self.evaluate_fn is None:
            # No evaluation function provided
            return None
        parameters_ndarrays = parameters_to_ndarrays(parameters)
        eval_res = self.evaluate_fn(server_round, parameters_ndarrays, {})
        if eval_res is None:
            return None
        loss, metrics = eval_res
        return loss, metrics
