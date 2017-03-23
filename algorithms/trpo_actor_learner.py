# -*- coding: utf-8 -*-
import time
import utils.ops
import utils.stats
import numpy as np
import utils.logger
import tensorflow as tf

from .policy_based_actor_learner import BaseA3CLearner
from networks.policy_v_network import PolicyValueNetwork


logger = utils.logger.getLogger('trpo_actor_learner')


class TRPOLearner(BaseA3CLearner):
	'''
	Implementation of Trust Region Policy Optimization + Generalized Advantage Estimation 
	as described in https://arxiv.org/pdf/1506.02438.pdf

	∂'π = F^-1 ∂π where F is the Fischer Information Matrix
	We can't compute F^-1 directly except for very small networks
	so we'll use either conjugate gradient descent to approximate F^-1 ∂π
	'''

	def __init__(self, args):
		args.entropy_regularisation_strength = 0.0
		super(TRPOLearner, self).__init__(args)

		policy_conf = {'name': 'local_learning_{}'.format(self.actor_id),
					   'input_shape': self.input_shape,
					   'num_act': self.num_actions,
					   'args': args}

		#we use separate networks as in the paper since so we don't do damage to the trust region updates
		self.policy_network = PolicyValueNetwork(policy_conf, use_value_head=False)
		self.local_network = self.policy_network
		#self.value_network = PolicyValueNetwork(value_conf, use_policy_head=False)

		if self.is_master():
			var_list = self.policy_network.params #+self.value_network.params
			self.saver = tf.train.Saver(var_list=var_list, max_to_keep=3,
                                        keep_checkpoint_every_n_hours=2)

		self.batch_size = 512
		self.num_epochs = 1000
		self.cg_damping = 0.001
		self.cg_subsample = 0.1
		self.max_kl = args.max_kl
		self.max_rollout = args.max_rollout
		self.episodes_per_batch = args.trpo_episodes
		self.episode_counter = args.episode_counter
		self.queue = args.queue
		self._build_ops()


	def _build_ops(self):
		eps = 1e-10
		self.action_probs = self.policy_network.output_layer_pi
		self.old_action_probs = tf.placeholder(tf.float32, shape=[None, self.num_actions], name='old_action_probs')

		action = tf.cast(tf.argmax(self.policy_network.selected_action_ph, axis=1), tf.int32)

		batch_idx = tf.range(0, tf.shape(action)[0])
		selected_prob = utils.ops.slice_2d(self.action_probs, batch_idx, action)
		old_selected_prob = utils.ops.slice_2d(self.old_action_probs, batch_idx, action)
		self.policy_loss = -tf.reduce_mean(tf.multiply(
			self.policy_network.adv_actor_ph,
			selected_prob / old_selected_prob
		))

		self.theta = utils.ops.flatten_vars(self.policy_network.params)
		self.kl = utils.stats.mean_kl_divergence_op(self.old_action_probs, self.action_probs)
		self.pg = utils.ops.flatten_vars(
			tf.gradients(self.policy_loss, self.policy_network.params))

		kl_firstfixed = tf.reduce_mean(tf.reduce_sum(tf.multiply(
			tf.stop_gradient(self.action_probs),
			tf.log(tf.stop_gradient(self.action_probs + eps) / (self.action_probs + eps))
		), axis=1))

		kl_grads = tf.gradients(kl_firstfixed, self.policy_network.params)
		flat_kl_grads = utils.ops.flatten_vars(kl_grads)

		self.pg_placeholder = tf.placeholder(tf.float32, shape=self.pg.get_shape().as_list(), name='pg_placeholder')
		self.fullstep, self.neggdotstepdir = self._conjugate_gradient_ops(-self.pg_placeholder, flat_kl_grads)


	def _conjugate_gradient_ops(self, pg_grads, kl_grads, max_iterations=20, residual_tol=1e-10):
		'''
		Construct conjugate gradient descent algorithm inside computation graph for improved efficiency
		'''
		i0 = tf.constant(0, dtype=tf.int32)
		loop_condition = lambda i, r, p, x, rdotr: tf.logical_or(
			tf.less(rdotr, residual_tol), tf.less(i, max_iterations))


		def body(i, r, p, x, rdotr):
			fvp = utils.ops.flatten_vars(tf.gradients(
				tf.reduce_sum(tf.stop_gradient(p)*kl_grads),
				self.policy_network.params))

			z = fvp + self.cg_damping * p

			alpha = rdotr / (tf.reduce_sum(p*z) + 1e-8)
			x += alpha * p
			r -= alpha * z

			new_rdotr = tf.reduce_sum(r*r)
			beta = new_rdotr / (rdotr + 1e-8)
			p = r + beta * p

			return i+1, r, p, x, new_rdotr

		_, r, p, stepdir, rdotr = tf.while_loop(
			loop_condition,
			body,
			loop_vars=[i0,
					   pg_grads,
					   pg_grads,
					   tf.zeros_like(pg_grads),
					   tf.reduce_sum(pg_grads*pg_grads)])

		fvp = utils.ops.flatten_vars(tf.gradients(
			tf.reduce_sum(tf.stop_gradient(stepdir)*kl_grads),
			self.policy_network.params)) + self.cg_damping * stepdir

		shs = 0.5 * tf.reduce_sum(stepdir*fvp)
		fullstep = stepdir * tf.sqrt(2.0 * self.max_kl / shs)
		neggdotstepdir = -tf.reduce_sum(pg_grads*stepdir)

		return fullstep, -neggdotstepdir


	#TODO: move this into utils so it can be used with other algorithms
	def compute_gae(self, rewards, values, next_val):
		values = values + [next_val]

		adv_batch = list()
		for i in range(len(rewards)):
			gae = 0.0
			for j in range(i, len(rewards)):
				TD_i = rewards[j] + self.gamma*values[j+1] - values[j]
				gae += TD_i * (self.gamma*self.td_lambda)**(j - i)

			adv_batch.append(gae)

		return adv_batch


	def choose_next_action(self, state):
		action_probs = self.session.run(
			self.policy_network.output_layer_pi,
			feed_dict={self.policy_network.input_ph: [state]})
            
		action_probs = action_probs.reshape(-1)

		action_index = self.sample_policy_action(action_probs)
		new_action = np.zeros([self.num_actions])
		new_action[action_index] = 1

		return new_action, action_probs


	def run_minibatches(self, data, *ops):
		outputs = [np.zeros(op.get_shape().as_list(), dtype=np.float32) for op in ops]

		data_size = len(data['state'])
		for start in range(0, data_size, self.batch_size):
			end = start + np.minimum(self.batch_size, data_size-start)
			feed_dict={
				self.policy_network.input_ph:           data['state'][start:end],
				self.policy_network.selected_action_ph: data['action'][start:end],
				self.policy_network.adv_actor_ph:       data['reward'][start:end],
				self.old_action_probs:                  data['pi'][start:end]
			}
			for i, output_i in enumerate(self.session.run(ops, feed_dict=feed_dict)):
				outputs[i] += output_i * (end-start)/float(data_size)

		return outputs


	#There doesn't seem to be a clean way to build the line search into the computation graph
	#so we'll have to hop back and forth between cpu and gpu each iteration
	def linesearch(self, data, x, fullstep):
		max_backtracks = 10

		fval = self.run_minibatches(data, self.policy_loss)

		for (_n_backtracks, stepfrac) in enumerate(.5**np.arange(max_backtracks)):
			xnew = x + stepfrac * fullstep
			self.assign_vars(self.policy_network, xnew)
			newfval, kl = self.run_minibatches(data, self.policy_loss, self.kl)

			improvement = fval - newfval
			logger.debug('Improvement {} / Mean KL {}'.format(improvement, kl))

			if improvement > 0 and kl < self.max_kl:
				return xnew

		logger.debug('No update')
		return x


	def update_grads(self, data):
		#we need to compute the policy gradient in minibatches to avoid GPU OOM errors on Atari
		pg = self.run_minibatches(data, self.pg)[0]

		#subsample data for conjugate gradient step
		# subsample = np.random.choice(len(data['state']), self.batch_size, replace=False)
		data_size = len(data['state'])
		subsample = np.random.choice(data_size, int(data_size*self.cg_subsample), replace=False)
		feed_dict={
			self.policy_network.input_ph:           data['state'][subsample],
			self.policy_network.selected_action_ph: data['action'][subsample],
			self.policy_network.adv_actor_ph:       data['reward'][subsample],
			self.old_action_probs:                  data['pi'][subsample],
			self.pg_placeholder:                    pg
		}
		theta_prev, fullstep, neggdotstepdir = self.session.run(
			[self.theta, self.fullstep, self.neggdotstepdir], feed_dict=feed_dict)

		new_theta = self.linesearch(data, theta_prev, fullstep)
		self.assign_vars(self.policy_network, new_theta)

		return self.session.run(self.kl, feed_dict)


	def _run_worker(self):		
		while True:
			if self.episode_counter.value() >= self.episodes_per_batch:
				time.sleep(.01)
				continue

			self.sync_net_with_shared_memory(self.local_network, self.learning_vars)
			s = self.emulator.get_initial_state()

			data = {
				'state':  list(),
				'pi':     list(),
				'action': list(),
				'reward': list(),
			}
			episode_over = False
			accumulated_rewards = list()
			while not episode_over and len(accumulated_rewards) < self.max_rollout:
				a, pi = self.choose_next_action(s)
				new_s, reward, episode_over = self.emulator.next(a)
				accumulated_rewards.append(self.rescale_reward(reward))

				data['state'].append(s)
				data['pi'].append(pi)
				data['action'].append(a)

				s = new_s

			mc_returns = list()
			running_total = 0.0
			for r in reversed(accumulated_rewards):
				running_total = r + self.gamma*running_total
				mc_returns.insert(0, running_total)

			data['reward'].extend(mc_returns)
			episode_reward = sum(accumulated_rewards)
			logger.debug('T{} / Episode Reward {}'.format(
				self.actor_id, episode_reward))

			self.queue.put((data, episode_reward))
			self.episode_counter.increment()
			

	def _run_master(self):
		for epoch in range(self.num_epochs):
			data = {
				'state':  list(),
				'pi':     list(),
				'action': list(),
				'reward': list(),
			}
			#collect worker experience
			episode_rewards = list()
			for _ in range(self.episodes_per_batch):
				worker_data, reward = self.queue.get()
				episode_rewards.append(reward)
				for key, value in list(worker_data.items()):
					data[key].extend(value)
					
			kl = self.update_grads({
				k: np.array(v) for k, v in list(data.items())})
			self.update_shared_memory()

			#discard old data
			while not self.queue.empty(): self.queue.get()
			self.episode_counter.set_value(0)

			mean_episode_reward = np.array(episode_rewards).mean()
			logger.info('Epoch {} / Mean KL Divergence {} / Mean Reward {}'.format(
				epoch+1, kl, mean_episode_reward))


	def _run(self):
		if self.is_master():
			try:
				self._run_master()
			except KeyboardInterrupt:
				while not self.queue.empty():
					self.queue.get()
		else:
			self._run_worker()



