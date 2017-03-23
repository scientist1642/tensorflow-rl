# -*- coding: utf-8 -*-
import numpy as np
import utils.logger
import tensorflow as tf
from networks.policy_v_network import PolicyNetwork
from .policy_based_actor_learner import BaseA3CLearner


logger = utils.logger.getLogger('cross_entropy_actor_learner')


class CEMLearner(BaseA3CLearner):
	'''
	Implementation of Cross-Entropy Method, Useful as a baseline for simple environments
	'''
	def __init__(self, args):
		super(CEMLearner, self).__init__(args)

		policy_conf = {'name': 'local_learning_{}'.format(self.actor_id),
					   'input_shape': self.input_shape,
					   'num_act': self.num_actions,
					   'args': args}

		self.local_network = PolicyNetwork(policy_conf)
		self.num_params = np.sum([
			np.prod(v.get_shape().as_list())
			for v in self.local_network.params])

		logger.info('Parameter count: {}'.format(self.num_params))
		self.mu = np.zeros(self.num_params)
		self.sigma = np.ones(self.num_params)

		if self.is_master():
			var_list = self.local_network.params
			self.saver = tf.train.Saver(var_list=var_list, max_to_keep=3,
                                        keep_checkpoint_every_n_hours=2)


	def choose_next_action(self, state):
		action_probs = self.session.run(
			self.local_network.output_layer_pi,
			feed_dict={self.local_network.input_ph: [state]})
            
		action_probs = action_probs.reshape(-1)

		action_index = self.sample_policy_action(action_probs)
		new_action = np.zeros([self.num_actions])
		new_action[action_index] = 1

		return new_action, action_probs


	def sample_theta(self, N):
		return self.mu + np.random.randn(N, self.num_params)*self.sigma


	def update_sample_distribution(self, population, rewards, keep_ratio=0.2):
		num_to_keep = int(keep_ratio * len(population))
		elite = np.array(rewards).argsort()[-num_to_keep:]

		self.mu = population[elite].mean(axis=0)
		self.sigma = population[elite].std(axis=0)

		return np.array(rewards)[elite].mean()


	def _run(self):
		for epoch in range(100):
			num_samples = 25
			episode_rewards = list()
			population = self.sample_theta(num_samples)

			for theta in population:
				self.assign_vars(self.local_network, theta)
				s = self.emulator.get_initial_state()

				total_episode_reward = 0.0
				episode_over = False

				while not episode_over:
					a, pi = self.choose_next_action(s)
					s, reward, episode_over = self.emulator.next(a)
					total_episode_reward += reward

				episode_rewards.append(total_episode_reward)

			population_mean_reward = np.array(episode_rewards).mean()
			elite_mean_reward = self.update_sample_distribution(population, episode_rewards)
			logger.info('Epoch {} / Population Mean {} / Elite Mean {}'.format(
				epoch+1, population_mean_reward, elite_mean_reward))


		