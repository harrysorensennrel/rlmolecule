import logging
from typing import Tuple

import numpy as np

import gym

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


ACTION_MAP = {0: (0, 1), 1: (1, 0), 2: (0, -1), 3: (-1, 0)}
OBSTACLE_CHANNEL = 0
GOAL_CHANNEL = 1
PLAYER_CHANNEL = 2


class GridWorldEnv(gym.Env):
    
    """Class implementing a square gridworld with 4-d discrete action space.

    The obs is modeled as an 3-channel array with separate channels for 
    obstacles (0), goal (1), and player (2).  The env is instantiated by 
    passing a size x size x 3 numpy array with 1 indicating presence of the
    object, and 0 otherwise.
    """

    def __init__(self,
                 grid: np.ndarray,
                 max_episode_steps: int = None,
                 sparse_rewards: bool = False,
                 obs_type: str = "scalar"):

        assert obs_type in ["rgb", "grayscale", "scalar", "index"]
        self.obs_type = obs_type

        self.size = grid.shape[1]
        self.max_episode_steps = max_episode_steps if max_episode_steps is not None \
            else 4 * (self.size - 1)
        self.sparse_rewards = sparse_rewards

        self.episode_steps = None
        self.cumulative_reward = None

        self.start = tuple([x[0] for x in np.where(grid[:, :, PLAYER_CHANNEL])])
        self.goal = tuple([x[0] for x in np.where(grid[:, :, GOAL_CHANNEL])])
        self.initial_grid = grid.copy()

        if self.obs_type == "index":
            self.obs_shape = [1]
            high = self.size * self.size - 1
            dtype = np.int64
        if self.obs_type == "scalar":
            self.obs_shape = [2]
            high = self.size
            dtype = np.int64
        if self.obs_type == "rgb":
            self.obs_shape = list(self.initial_grid.shape)
            high = 1.
            dtype = np.float64
        if self.obs_type == "grayscale":
            self.obs_shape = list(self.initial_grid.shape)
            self.obs_shape[-1] = 1
            high = 1.
            dtype = np.float64
        self.obs_shape = tuple(self.obs_shape)

        self.observation_space = gym.spaces.Box(
            low=0, high=high, shape=self.obs_shape, dtype=dtype)

        self.action_space = gym.spaces.Discrete(4)


    def reset(self) -> np.ndarray:
        self.episode_steps = 0
        self.cumulative_reward = 0.
        self.player = list(self.start)
        self.grid = self.initial_grid.copy()
        return self.get_obs()


    def get_obs(self) -> np.ndarray:
        obs = self.grid.copy()
        if self.obs_type == "index":
            obs = np.where(obs[:, :, PLAYER_CHANNEL].squeeze())
            obs = np.array([obs[0]*self.size + obs[1]], dtype=np.int64).reshape(1)
        if self.obs_type == "scalar":
            obs = np.where(obs[:, :, PLAYER_CHANNEL].squeeze())
            obs = np.array([obs[0], obs[1]], dtype=np.int64).reshape(2)
        if self.obs_type == "grayscale":
            obs[0, :, :] *= 1/3.
            obs[1, :, :] *= 2/3.
            obs = np.sum(obs, axis=-1).reshape(self.obs_shape)
        return obs.copy()
        

    def step(self, action: int) -> Tuple[np.ndarray, float, bool, dict]:

        # Zero out the previous player cell
        self.grid[self.player[0], self.player[1], PLAYER_CHANNEL] = 0.
        
        # Update the new player cell, keeping it within the grid boundaries
        action = ACTION_MAP[action]
        _row = min(self.size-1, max(0, self.player[0] + action[0]))
        _col = min(self.size-1, max(0, self.player[1] + action[1]))

        # If the new position is open, update player position, else do nothing
        if self.grid[_row, _col, OBSTACLE_CHANNEL] == 0:
            self.player = (_row, _col)
        self.grid[self.player[0], self.player[1], PLAYER_CHANNEL] = 1.

        # Look for termination
        goal_reached = tuple(self.player) == self.goal        
        self.episode_steps += 1
        max_steps_reached = self.episode_steps == self.max_episode_steps
        done = goal_reached or max_steps_reached

        # Compute reward
        reward = -1
        if done:
            reward += self.get_terminal_reward()
        self.cumulative_reward += reward

        # If using sparse rewards, only return cumulative reward if done, else 0.
        if self.sparse_rewards:
            reward = self.cumulative_reward if done else 0.

        return self.get_obs(), reward, done, {}


    def get_terminal_reward(self) -> float:
        # Terminal reward results in max cumulative reward being 0.
        return -self._tuple_distance(self.player, self.goal) + 2 * (self.size - 1)

    def _tuple_distance(self, t1, t2) -> float:
        """Compute the manhattan distance between two tuples."""
        return np.sum(np.abs((np.array(t1) - np.array(t2))))


def make_empty_grid(size=5):
    """Helper function for creating empty (no obstacles) of given size."""
    grid = np.zeros((size, size, 3))
    grid[0, 0, PLAYER_CHANNEL] = 1
    grid[-1, -1, GOAL_CHANNEL] = 1
    return grid


def make_doorway_grid():
    """Example where wall blocks all but 2 pixels for the player to pass through."""
    size = 10
    grid = np.zeros((size, size, 3))

    obstacle_idx = np.ones((8, 3), dtype=int)
    obstacle_idx[:, 0] = 4
    obstacle_idx[:, 1] = [x for x in range(10) if x not in [4, 5]]
    obstacle_idx[:, 2] = OBSTACLE_CHANNEL

    grid[obstacle_idx[:, 0], obstacle_idx[:, 1], obstacle_idx[:, 2]] = 1
    grid[0, 0, PLAYER_CHANNEL] = 1
    grid[-1, -1, GOAL_CHANNEL] = 1

    return grid


def policy(env):
    """An optimal policy for empty gridworld: find the vector pointing towards
    the goal, and choose the first non-zero direction.  Total episode reward
    should be 0 when running this."""
    goal_direction = np.array(env.goal) - np.array(env.player)
    print("goal direction", goal_direction)
    action = np.where(goal_direction.squeeze() != 0)[0][0]
    if action == 0:
        if goal_direction[action] > 0:
            return 1
        return 3
    if goal_direction[action] > 0:
        return 0
    return 2


if __name__ == "__main__":
    
    # from tf_model import gridworld_image_embed_policy
    # model = gridworld_image_embed_policy(
    #     size=32,
    #     filters=[4, 8, 16],
    #     kernel_size=[8, 2, 2],
    #     strides=[8, 2, 1]
    # )

    size = 64
    grid = make_empty_grid(size=size)
    env = GridWorldEnv(grid, obs_type="scalar", max_episode_steps=2*size+2)
    obs = env.reset()

    print("obs", obs)
    #print("PREDICT", model.predict(obs.reshape(1, 1)))
    done, rew, step = False, 0., 0
    while not done:
        #action = env.action_space.sample()
        #action = 2
        action = policy(env)
        obs, r, done, _ = env.step(action)
        rew += r
        step += 1
        print("\nstep {}, reward {}, done {}".format(step, r, done))
        print("action", action)
        print("obs", obs)
        #print("policy", model.predict(obs.reshape(1, 1)))
        
    print("final reward", rew)
