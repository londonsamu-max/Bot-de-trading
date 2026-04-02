"""
PPO Agent Wrapper

Wraps stable-baselines3 PPO for training and inference.
Handles model creation, training, loading, and prediction.
"""

import logging
from pathlib import Path
from typing import Optional

import numpy as np
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

from src.agent.trading_env import TradingEnv

logger = logging.getLogger(__name__)


class PPOAgent:
    """
    PPO trading agent wrapper.

    Manages the lifecycle: create -> train -> save -> load -> predict.
    One agent per instrument (NAS100, US30, XAUUSD).
    """

    def __init__(self, symbol: str, model_dir: str = "models/"):
        self.symbol = symbol
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.model: Optional[PPO] = None
        self.vec_normalize: Optional[VecNormalize] = None

    def create(self, env_kwargs: dict, hyperparams: dict) -> PPO:
        """
        Create a new PPO model with vectorized environment.

        Args:
            env_kwargs: Arguments for TradingEnv
            hyperparams: PPO hyperparameters from config
        """
        vec_env = DummyVecEnv([lambda: TradingEnv(**env_kwargs)])
        vec_env = VecNormalize(
            vec_env, norm_obs=True, norm_reward=True, clip_obs=10.0,
        )
        self.vec_normalize = vec_env

        net_arch = hyperparams.get("net_arch", {"pi": [256, 128, 64], "vf": [256, 128, 64]})
        policy_kwargs = {"net_arch": net_arch}

        self.model = PPO(
            policy=hyperparams.get("policy", "MlpPolicy"),
            env=vec_env,
            learning_rate=hyperparams.get("learning_rate", 3e-4),
            n_steps=hyperparams.get("n_steps", 2048),
            batch_size=hyperparams.get("batch_size", 64),
            n_epochs=hyperparams.get("n_epochs", 10),
            gamma=hyperparams.get("gamma", 0.99),
            gae_lambda=hyperparams.get("gae_lambda", 0.95),
            clip_range=hyperparams.get("clip_range", 0.2),
            ent_coef=hyperparams.get("ent_coef", 0.01),
            vf_coef=hyperparams.get("vf_coef", 0.5),
            max_grad_norm=hyperparams.get("max_grad_norm", 0.5),
            policy_kwargs=policy_kwargs,
            verbose=1,
            tensorboard_log=f"logs/tensorboard/{self.symbol}/",
        )
        logger.info(f"PPO model created for {self.symbol}")
        return self.model

    def train(self, total_timesteps: int, callbacks: list = None):
        """Train the model."""
        if self.model is None:
            raise RuntimeError("Model not created. Call create() first.")

        logger.info(f"Training {self.symbol} for {total_timesteps} timesteps")
        self.model.learn(
            total_timesteps=total_timesteps,
            callback=callbacks,
            progress_bar=True,
        )
        logger.info(f"Training complete for {self.symbol}")

    def save(self, version: str = "v1"):
        """Save model and normalization stats."""
        if self.model is None:
            raise RuntimeError("No model to save")

        model_path = self.model_dir / f"ppo_{self.symbol}_{version}"
        norm_path = self.model_dir / f"vecnormalize_{self.symbol}_{version}.pkl"

        self.model.save(str(model_path))
        if self.vec_normalize:
            self.vec_normalize.save(str(norm_path))

        logger.info(f"Model saved: {model_path}")

    def load(self, version: str = "v1"):
        """Load model and normalization stats."""
        model_path = self.model_dir / f"ppo_{self.symbol}_{version}.zip"
        norm_path = self.model_dir / f"vecnormalize_{self.symbol}_{version}.pkl"

        if not model_path.exists():
            raise FileNotFoundError(f"Model not found: {model_path}")

        # Create a dummy env for loading
        dummy_env = DummyVecEnv([lambda: TradingEnv()])

        if norm_path.exists():
            self.vec_normalize = VecNormalize.load(str(norm_path), dummy_env)
            self.vec_normalize.training = False
            self.vec_normalize.norm_reward = False
        else:
            self.vec_normalize = VecNormalize(dummy_env, norm_obs=False, norm_reward=False)

        self.model = PPO.load(str(model_path), env=self.vec_normalize)
        logger.info(f"Model loaded: {model_path}")

    def predict(self, observation: np.ndarray, deterministic: bool = True) -> tuple[int, dict]:
        """
        Get action from the trained model.

        Args:
            observation: Raw 46-dim observation vector
            deterministic: True for live trading, False for exploration

        Returns:
            (action, info_dict)
        """
        if self.model is None:
            raise RuntimeError("No model loaded. Call load() or create()+train() first.")

        # Normalize observation using saved stats
        if self.vec_normalize and self.vec_normalize.norm_obs:
            obs = self.vec_normalize.normalize_obs(observation.reshape(1, -1))
        else:
            obs = observation.reshape(1, -1)

        action, _states = self.model.predict(obs, deterministic=deterministic)
        action_int = int(action[0]) if hasattr(action, '__len__') else int(action)

        action_names = {0: "HOLD", 1: "BUY_SMALL", 2: "BUY_FULL", 3: "SELL_SMALL", 4: "SELL_FULL"}
        return action_int, {"action_name": action_names.get(action_int, "UNKNOWN")}
