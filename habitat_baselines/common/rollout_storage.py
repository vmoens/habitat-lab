#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import warnings

import numpy as np
import torch

from habitat_baselines.common.tensor_dict import TensorDict


class RolloutStorage:
    r"""Class for storing rollout information for RL trainers."""

    def __init__(
        self,
        numsteps,
        num_envs,
        observation_space,
        action_space,
        recurrent_hidden_state_size,
        num_recurrent_layers=1,
        is_double_buffered: bool = False,
    ):
        self.buffers = TensorDict()
        self.buffers["observations"] = TensorDict()

        for sensor in observation_space.spaces:
            self.buffers["observations"][sensor] = torch.from_numpy(
                np.zeros(
                    (
                        numsteps + 1,
                        num_envs,
                        *observation_space.spaces[sensor].shape,
                    ),
                    dtype=observation_space.spaces[sensor].dtype,
                )
            )

        self.buffers["recurrent_hidden_states"] = torch.zeros(
            numsteps + 1,
            num_envs,
            num_recurrent_layers,
            recurrent_hidden_state_size,
        )

        self.buffers["rewards"] = torch.zeros(numsteps + 1, num_envs, 1)
        self.buffers["value_preds"] = torch.zeros(numsteps + 1, num_envs, 1)
        self.buffers["returns"] = torch.zeros(numsteps + 1, num_envs, 1)

        self.buffers["action_log_probs"] = torch.zeros(
            numsteps + 1, num_envs, 1
        )
        if action_space.__class__.__name__ == "ActionSpace":
            action_shape = 1
        else:
            action_shape = action_space.shape[0]

        self.buffers["actions"] = torch.zeros(
            numsteps + 1, num_envs, action_shape
        )
        self.buffers["prev_actions"] = torch.zeros(
            numsteps + 1, num_envs, action_shape
        )
        if action_space.__class__.__name__ == "ActionSpace":
            self.buffers["actions"] = self.buffers["actions"].long()
            self.buffers["prev_actions"] = self.buffers["prev_actions"].long()

        self.buffers["masks"] = torch.zeros(
            numsteps + 1, num_envs, 1, dtype=torch.bool
        )

        self.is_double_buffered = is_double_buffered
        self._nbuffers = 2 if is_double_buffered else 1
        self._num_envs = num_envs

        assert (self._num_envs % self._nbuffers) == 0

        self.numsteps = numsteps
        self.steps = [0 for _ in range(self._nbuffers)]

    @property
    def step(self):
        assert all(s == self.steps[0] for s in self.steps)
        return self.steps[0]

    def to(self, device):
        self.buffers.map_in_place(lambda v: v.to(device))

    def insert(
        self,
        observations=None,
        recurrent_hidden_states=None,
        actions=None,
        action_log_probs=None,
        value_preds=None,
        rewards=None,
        masks=None,
        buffer_index: int = 0,
    ):
        if not self.is_double_buffered:
            assert buffer_index == 0

        next_step = dict(
            observations=observations,
            recurrent_hidden_states=recurrent_hidden_states,
            prev_actions=actions,
            masks=masks,
        )

        current_step = dict(
            actions=actions, action_log_probs=action_log_probs, rewards=rewards
        )

        next_step = {k: v for k, v in next_step.items() if v is not None}
        current_step = {k: v for k, v in current_step.items() if v is not None}

        env_slice = slice(
            int(buffer_index * self._num_envs / self._nbuffers),
            int((buffer_index + 1) * self._num_envs / self._nbuffers),
        )

        if len(next_step) > 0:
            self.buffers.set(
                (self.steps[buffer_index] + 1, env_slice),
                next_step,
                strict=False,
            )

        if len(current_step) > 0:
            self.buffers.set(
                (self.steps[buffer_index], env_slice),
                current_step,
                strict=False,
            )

    def advance_rollout(self, buffer_index: int = 0):
        self.steps[buffer_index] += 1

    def after_update(self):
        self.buffers[0] = self.buffers[self.step]

        self.steps = [0 for _ in self.steps]

    def compute_returns(self, next_value, use_gae, gamma, tau):
        if use_gae:
            self.buffers["value_preds"][self.step] = next_value
            gae = 0
            for step in reversed(range(self.step)):
                delta = (
                    self.buffers["rewards"][step]
                    + gamma
                    * self.buffers["value_preds"][step + 1]
                    * self.buffers["masks"][step + 1]
                    - self.buffers["value_preds"][step]
                )
                gae = (
                    delta + gamma * tau * gae * self.buffers["masks"][step + 1]
                )
                self.buffers["returns"][step] = (
                    gae + self.buffers["value_preds"][step]
                )
        else:
            self.buffers["returns"][self.step] = next_value
            for step in reversed(range(self.step)):
                self.buffers["returns"][step] = (
                    gamma
                    * self.buffers["returns"][step + 1]
                    * self.buffers["masks"][step + 1]
                    + self.buffers["rewards"][step]
                )

    def recurrent_generator(self, advantages, num_mini_batch) -> TensorDict:
        num_simulators = advantages.size(1)
        assert num_simulators >= num_mini_batch, (
            "Trainer requires the number of simulators ({}) "
            "to be greater than or equal to the number of "
            "trainer mini batches ({}).".format(num_simulators, num_mini_batch)
        )
        if num_simulators % num_mini_batch != 0:
            warnings.warn(
                "Number of simulators ({}) is not a multiple of the number of mini batches ({})".format(
                    num_simulators, num_mini_batch
                )
            )
        for inds in torch.randperm(num_simulators).chunk(num_mini_batch):
            batch = self.buffers[0 : self.step, inds]
            batch["advantages"] = advantages[0 : self.step, inds]
            batch["recurrent_hidden_states"] = batch[
                "recurrent_hidden_states"
            ][0:1]

            yield batch.map(lambda v: v.flatten(0, 1))
