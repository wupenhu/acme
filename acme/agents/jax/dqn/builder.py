# Copyright 2018 DeepMind Technologies Limited. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""DQN Builder."""
from typing import Callable, Iterator, List, Optional

from acme import adders
from acme import core
from acme import specs
from acme.adders import reverb as adders_reverb
from acme.agents import builders
from acme.agents.jax import actors
from acme.agents.jax.dqn import config as dqn_config
from acme.agents.jax.dqn import learning_lib
from acme.datasets import reverb as datasets
from acme.jax import networks as networks_lib
from acme.jax import variable_utils
from acme.utils import counting
from acme.utils import loggers
import jax
import jax.numpy as jnp
import optax
import reverb
from reverb import rate_limiters
import rlax


def default_behavior_policy(network: networks_lib.FeedForwardNetwork,
                            epsilon: float,
                            params: networks_lib.Params,
                            key: networks_lib.PRNGKey,
                            observation: networks_lib.Observation):
  """Returns an action for the given observation."""
  action_values = network.apply(params, observation)
  actions = rlax.epsilon_greedy(epsilon).sample(key, action_values)
  return actions.astype(jnp.int32)


class DQNBuilder(builders.ActorLearnerBuilder):
  """DQN Builder."""

  def __init__(
      self,
      config: dqn_config.DQNConfig,
      loss_fn: learning_lib.LossFn,
      logger_fn: Callable[[], loggers.Logger] = lambda: None,
  ):
    """Creates DQN learner and the behavior policies.

    Args:
      config: DQN config.
      loss_fn: A loss function.
      logger_fn: a logger factory for the learner
    """
    self._random_key = jax.random.PRNGKey(config.seed)
    self._config = config
    self._loss_fn = loss_fn
    self._logger_fn = logger_fn

  def make_learner(
      self,
      networks: networks_lib.FeedForwardNetwork,
      dataset: Iterator[reverb.ReplaySample],
      replay_client: Optional[reverb.Client] = None,
      counter: Optional[counting.Counter] = None,
      checkpoint: bool = False,
  ) -> core.Learner:
    key, self._random_key = jax.random.split(self._random_key)
    return learning_lib.SGDLearner(
        network=networks,
        random_key=key,
        optimizer=optax.adam(self._config.learning_rate),
        target_update_period=self._config.target_update_period,
        data_iterator=dataset,
        loss_fn=self._loss_fn,
        replay_client=None,
        counter=counter,
        num_sgd_steps_per_step=self._config.num_sgd_steps_per_step,
        logger=self._logger_fn())

  def make_actor(
      self,
      policy_network,
      adder: Optional[adders.Adder] = None,
      variable_source: Optional[core.VariableSource] = None,
  ) -> core.Actor:
    key, self._random_key = jax.random.split(self._random_key)
    return actors.FeedForwardActor(
        policy=policy_network,
        random_key=key,
        # Inference happens on CPU, so it's better to move variables there too.
        variable_client=variable_utils.VariableClient(variable_source, '',
                                                      device='cpu'),
        adder=adder,
    )

  def make_replay_tables(
      self, environment_spec: specs.EnvironmentSpec) -> List[reverb.Table]:
    """Creates reverb tables for the algorithm."""
    samples_per_insert_tolerance = (
        self._config.samples_per_insert_tolerance_rate *
        self._config.samples_per_insert)
    error_buffer = self._config.min_replay_size * samples_per_insert_tolerance
    limiter = rate_limiters.SampleToInsertRatio(
        min_size_to_sample=self._config.min_replay_size,
        samples_per_insert=self._config.samples_per_insert,
        error_buffer=error_buffer)
    return [reverb.Table(
        name=self._config.replay_table_name,
        sampler=reverb.selectors.Prioritized(self._config.priority_exponent),
        remover=reverb.selectors.Fifo(),
        max_size=self._config.max_replay_size,
        rate_limiter=limiter,
        signature=adders_reverb.NStepTransitionAdder.signature(
            environment_spec))]

  def make_dataset_iterator(
      self, replay_client: reverb.Client) -> Iterator[reverb.ReplaySample]:
    """Creates a dataset iterator to use for learning."""
    dataset = datasets.make_reverb_dataset(
        table=self._config.replay_table_name,
        server_address=replay_client.server_address,
        batch_size=(
            self._config.batch_size * self._config.num_sgd_steps_per_step),
        prefetch_size=self._config.prefetch_size,
        transition_adder=True)
    return dataset.as_numpy_iterator()

  def make_adder(self, replay_client: reverb.Client) -> adders.Adder:
    """Creates an adder which handles observations."""
    return adders_reverb.NStepTransitionAdder(
        priority_fns={self._config.replay_table_name: None},
        client=replay_client,
        n_step=self._config.n_step,
        discount=self._config.discount)

