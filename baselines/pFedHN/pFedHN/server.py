"""Server for pFedHN, pFedHNPC .

Here the HyperNetwork performs the necessary steps
"""
import concurrent.futures
import json
import timeit
from collections import OrderedDict
from logging import DEBUG, INFO
from typing import List, Optional, Tuple, Union

import torch
from flwr.common import (
    Code,
    EvaluateIns,
    EvaluateRes,
    FitIns,
    FitRes,
    Parameters,
    ndarrays_to_parameters,
    parameters_to_ndarrays,
)
from flwr.common.logger import log
from flwr.server import Server
from flwr.server.client_manager import ClientManager
from flwr.server.client_proxy import ClientProxy
from flwr.server.history import History
from flwr.server.strategy import Strategy
from omegaconf import DictConfig

from pFedHN.models import CNNHyper
from pFedHN.utils import set_seed

FitResultsAndFailures = Tuple[
    List[Tuple[ClientProxy, FitRes]],
    List[Union[Tuple[ClientProxy, FitRes], BaseException]],
]
EvaluateResultsAndFailures = Tuple[
    List[Tuple[ClientProxy, EvaluateRes]],
    List[Union[Tuple[ClientProxy, EvaluateRes], BaseException]],
]


set_seed(42)


# pylint: disable=invalid-name
class pFedHNServer(Server):
    """HyperNetwork Server Implementation."""

    def __init__(
        self, client_manager: ClientManager, strategy: Strategy, cfg: DictConfig
    ):
        super().__init__(client_manager=client_manager, strategy=strategy)
        self.cfg = cfg
        self.hnet = CNNHyper(
            n_nodes=self.cfg.num_clients,
            # The dimension is given in page 6 of the paper
            # Under Section 5 - Training Stratergies.
            # Justification is given in Supplementary Material
            # Section C Additional Experiments - C.2.2
            embedding_dim=int(
                1 + self.cfg.num_clients / self.cfg.server.embedding_dim_denominator
            ),
            in_channels=self.cfg.model.in_channels,
            n_kernels=self.cfg.model.n_kernels,
            out_dim=self.cfg.model.out_dim,
            hidden_dim=100,
            n_hidden=self.cfg.model.n_hidden,
            local=self.cfg.model.local,
        )
        self.hnet.to(torch.device("cuda:0" if torch.cuda.is_available() else "cpu"))

        self.optimizer = torch.optim.SGD(
            [
                {
                    "params": [
                        p for n, p in self.hnet.named_parameters() if "embed" not in n
                    ]
                },
                {
                    "params": [
                        p for n, p in self.hnet.named_parameters() if "embed" in n
                    ],
                    "lr": self.cfg.server.lr,
                },
            ],
            lr=self.cfg.server.lr,
            momentum=self.cfg.server.momentum,
            weight_decay=self.cfg.server.wd,
        )

        self.parameters: Parameters = Parameters(
            tensors=[], tensor_type="numpy.ndarray"
        )
        self.loss: List = []
        self.accuracies: List = []
        self.data: List = []

    def evaluate_round(self, server_round: int, timeout: Optional[float]):
        """Perform federated evaluation."""
        # pylint: disable=too-many-locals
        self.hnet.eval()

        def weights_to_clients(client_id):
            weights = self.hnet(
                torch.tensor([client_id], dtype=torch.long).to(
                    torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
                )
            )
            return weights

        client_instructions = self.strategy.configure_evaluate(
            server_round=server_round,
            parameters=None,  # type: ignore[arg-type]
            client_manager=self._client_manager,
        )

        for client_instruction in client_instructions:
            client_id = client_instruction[0].cid
            client_weights = weights_to_clients(int(client_id))
            array = [val.cpu().detach().numpy() for _, val in client_weights.items()]
            parameters = ndarrays_to_parameters(array)
            client_instruction[1].parameters = parameters

        if not client_instructions:
            log(INFO, "evaluate_round %s: no clients selected, cancel", server_round)
            return None

        log(
            DEBUG,
            "evaluate_round %s: strategy sampled %s clients (out of %s)",
            server_round,
            len(client_instructions),
            self._client_manager.num_available(),
        )
        results, failures = evaluate_clients(
            client_instructions,
            max_workers=self.max_workers,
            timeout=timeout,
        )

        log(
            DEBUG,
            "evaluate_round %s received %s results and %s failures",
            server_round,
            len(results),
            len(failures),
        )

        aggregated_result = self.strategy.aggregate_evaluate(
            server_round, results, failures
        )
        avg_loss = aggregated_result[0]
        avg_acc = aggregated_result[1]["avg_acc"]

        data = {
            "round": server_round,
            "avg_loss": avg_loss,
            "avg_accuracy": avg_acc,
        }

        self.data.append(data)

        with open("pfedhn.json", "w", encoding="utf-8") as f:
            json.dump(self.data, f)

        log(
            DEBUG,
            "AvgLoss: %.4f, AvgAcc: %.4f",
            avg_loss,
            avg_acc,
        )

        return avg_loss, avg_acc, (results, failures)

    def fit_round(self, server_round: int, timeout: Optional[float]):
        """Perform a single round of federated averaging."""
        # pylint: disable=too-many-locals
        self.hnet.train()

        def weights_to_clients(client_id):
            weights = self.hnet(
                torch.tensor([client_id], dtype=torch.long).to(
                    torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
                )
            )
            return weights

        client_instructions = self.strategy.configure_fit(
            server_round=server_round,
            parameters=self.parameters,  # type: ignore[arg-type]
            client_manager=self._client_manager,
        )

        one_client = client_instructions[0]
        one_client_cid = one_client[0].cid

        one_client_weights = weights_to_clients(int(one_client_cid))

        inner_state = OrderedDict(
            {k: tensor.data for k, tensor in one_client_weights.items()}
        )

        array = [val.cpu().detach().numpy() for _, val in one_client_weights.items()]
        parameters = ndarrays_to_parameters(array)
        one_client[1].parameters = parameters

        if not client_instructions:
            log(INFO, "fit_round %s: no clients selected, cancel", server_round)
            return None

        log(
            DEBUG,
            "fit_round %s: strategy sampled %s clients (out of %s)",
            server_round,
            len(client_instructions),
            self._client_manager.num_available(),
        )
        results, failures = fit_clients(
            client_instructions=client_instructions,
            max_workers=self.max_workers,
            timeout=timeout,
        )
        log(
            DEBUG,
            "fit_round %s received %s results and %s failures",
            server_round,
            len(results),
            len(failures),
        )

        final_state_val, metrics_aggregated = self.strategy.aggregate_fit(
            server_round, results, failures
        )
        final_dict = zip(
            one_client_weights.keys(),
            parameters_to_ndarrays(final_state_val),  # type: ignore[arg-type]
        )

        final_state = OrderedDict(
            {
                k: torch.tensor(v).to(
                    torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
                )
                for k, v in final_dict
            }
        )
        delta_theta = OrderedDict(
            {k: inner_state[k] - final_state[k] for k in one_client_weights.keys()}
        )

        hnet_grads = torch.autograd.grad(
            list(one_client_weights.values()),
            self.hnet.parameters(),  # type: ignore[arg-type]
            grad_outputs=list(delta_theta.values()),
        )

        # update hnet weights
        for model_parameter, gradient in zip(self.hnet.parameters(), hnet_grads):
            model_parameter.grad = gradient

        torch.nn.utils.clip_grad_norm_(self.hnet.parameters(), 50)
        self.optimizer.step()

        ndarr = [val.cpu().numpy() for _, val in self.hnet.state_dict().items()]
        hnet_parameters = ndarrays_to_parameters(ndarr)

        return hnet_parameters, metrics_aggregated, (results, failures)

    def fit(self, num_rounds: int, timeout: Optional[float]):
        """Handle all the fit operations in server side."""
        # pylint: disable=too-many-locals
        history = History()
        log(INFO, "Hypernetwork is present in the server")
        log(INFO, "FL Starting")
        start_time = timeit.default_timer()

        for current_round in range(1, num_rounds + 1):
            # self.hnet.train()
            res_fit = self.fit_round(
                server_round=current_round,
                timeout=timeout,
            )
            if res_fit is not None:
                parameters_prime, fit_metrics, _ = res_fit  # type: ignore[has-type]
                if parameters_prime:
                    self.parameters = parameters_prime
                history.add_metrics_distributed_fit(
                    server_round=current_round, metrics=fit_metrics
                )

                # Evaluate the model
                res_cen = self.strategy.evaluate(
                    current_round, parameters=self.parameters
                )
                if res_cen is not None:
                    loss_cen, metrics_cen = res_cen
                    log(
                        INFO,
                        "fit progress: (%s, %s, %s, %s)",
                        current_round,
                        loss_cen,
                        metrics_cen,
                        timeit.default_timer() - start_time,
                    )
                    history.add_loss_centralized(
                        server_round=current_round, loss=loss_cen
                    )
                    history.add_metrics_centralized(
                        server_round=current_round, metrics=metrics_cen
                    )
            # self.hnet.eval()
            res_fed = self.evaluate_round(
                server_round=current_round,
                timeout=timeout,
            )
            if res_fed is not None:
                loss_fed, acc_fed, _ = res_fed
                if loss_fed is not None:
                    history.add_loss_distributed(
                        server_round=current_round, loss=loss_fed
                    )
                    history.add_metrics_distributed(
                        server_round=current_round, metrics={"avg_acc": acc_fed}
                    )

        end_time = timeit.default_timer()
        elapsed = end_time - start_time
        log(INFO, "FL finised in %s", elapsed)
        return history


def fit_clients(
    client_instructions: List[Tuple[ClientProxy, FitIns]],
    max_workers: Optional[int],
    timeout: Optional[float],
) -> FitResultsAndFailures:
    """Refine parameters concurrently on all selected clients."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        submitted_fs = {
            executor.submit(fit_client, client_proxy, ins, timeout)
            for client_proxy, ins in client_instructions
        }
        finished_fs, _ = concurrent.futures.wait(
            fs=submitted_fs,
            timeout=None,  # Handled in the respective communication stack
        )

    # Gather results
    results: List[Tuple[ClientProxy, FitRes]] = []
    failures: List[Union[Tuple[ClientProxy, FitRes], BaseException]] = []
    for future in finished_fs:
        _handle_finished_future_after_fit(
            future=future, results=results, failures=failures
        )
    return results, failures


def fit_client(
    client: ClientProxy, ins: FitIns, timeout: Optional[float]
) -> Tuple[ClientProxy, FitRes]:
    """Refine parameters on a single client."""
    fit_res = client.fit(ins, timeout=timeout)
    return client, fit_res


def _handle_finished_future_after_fit(
    future: concurrent.futures.Future,  # type: ignore
    results: List[Tuple[ClientProxy, FitRes]],
    failures: List[Union[Tuple[ClientProxy, FitRes], BaseException]],
) -> None:
    """Convert finished future into either a result or a failure."""
    # Check if there was an exception
    failure = future.exception()
    if failure is not None:
        failures.append(failure)
        return

    # Successfully received a result from a client
    result: Tuple[ClientProxy, FitRes] = future.result()
    _, res = result

    # Check result status code
    if res.status.code == Code.OK:
        results.append(result)
        return

    # Not successful, client returned a result where the status code is not OK
    failures.append(result)


def evaluate_clients(
    client_instructions: List[Tuple[ClientProxy, EvaluateIns]],
    max_workers: Optional[int],
    timeout: Optional[float],
) -> EvaluateResultsAndFailures:
    """Evaluate parameters concurrently on all selected clients."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        submitted_fs = {
            executor.submit(evaluate_client, client_proxy, ins, timeout)
            for client_proxy, ins in client_instructions
        }
        finished_fs, _ = concurrent.futures.wait(
            fs=submitted_fs,
            timeout=None,  # Handled in the respective communication stack
        )

    # Gather results
    results: List[Tuple[ClientProxy, EvaluateRes]] = []
    failures: List[Union[Tuple[ClientProxy, EvaluateRes], BaseException]] = []
    for future in finished_fs:
        _handle_finished_future_after_evaluate(
            future=future, results=results, failures=failures
        )
    return results, failures


def evaluate_client(
    client: ClientProxy,
    ins: EvaluateIns,
    timeout: Optional[float],
) -> Tuple[ClientProxy, EvaluateRes]:
    """Evaluate parameters on a single client."""
    evaluate_res = client.evaluate(ins, timeout=timeout)
    return client, evaluate_res


def _handle_finished_future_after_evaluate(
    future: concurrent.futures.Future,  # type: ignore
    results: List[Tuple[ClientProxy, EvaluateRes]],
    failures: List[Union[Tuple[ClientProxy, EvaluateRes], BaseException]],
) -> None:
    """Convert finished future into either a result or a failure."""
    # Check if there was an exception
    failure = future.exception()
    if failure is not None:
        failures.append(failure)
        return

    # Successfully received a result from a client
    result: Tuple[ClientProxy, EvaluateRes] = future.result()
    _, res = result

    # Check result status code
    if res.status.code == Code.OK:
        results.append(result)
        return

    # Not successful, client returned a result where the status code is not OK
    failures.append(result)
