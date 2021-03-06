import tensorflow as tf
tf.enable_eager_execution()

import os
from tensorflow.python import keras
from tensorflow.python.keras import layers
from queue import Queue
import threading
import gym
import multiprocessing
import numpy as np
import matplotlib.pyplot as plt

save_dir = '/Users/msun/Documents/saved-model/'
learning_rate=0.001
max_eps=1200
update_freq=20
gamma=0.99

def record(episode, episode_reward, worker_idx, global_ep_reward, result_queue, total_loss, num_steps):
    """Helper function to store score and print statistics.
      Arguments:
        episode: Current episode
        episode_reward: Reward accumulated over the current episode
        worker_idx: Which thread (worker)
        global_ep_reward: The moving average of the global reward
        result_queue: Queue storing the moving average of the scores
        total_loss: The total loss accumualted over the current episode
        num_steps: The number of steps the episode took to complete
      """
    if global_ep_reward == 0:
        global_ep_reward = episode_reward
    else:
        global_ep_reward = global_ep_reward * 0.99 + episode_reward * 0.01
    print(
        f"Episode: {episode} | "
        f"Moving Average Reward: {int(global_ep_reward)} | "
        f"Episode Reward: {int(episode_reward)} | "
        f"Loss: {int(total_loss / float(num_steps) * 1000) / 1000} | "
        f"Steps: {num_steps} | "
        f"Worker: {worker_idx}"
    )
    result_queue.put(global_ep_reward)
    return global_ep_reward

class Memory:
    def __init__(self):
        self.states = []
        self.actions = []
        self.rewards = []

    def store(self, state, action, reward):
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)

    def clear(self):
        self.states = []
        self.actions = []
        self.rewards = []


class ActorCriticModel(keras.Model):
    def __init__(self, state_size, action_size):
        super(ActorCriticModel, self).__init__()
        self.state_size = state_size
        self.action_size = action_size
        self.dense1 = layers.Dense(100, activation='relu')
        self.policy_logits = layers.Dense(action_size)
        self.dense2 = layers.Dense(100, activation='relu')
        self.values = layers.Dense(1)

    def call(self, inputs):
        # Forward pass
        x = self.dense1(inputs)
        logits = self.policy_logits(x)
        v1 = self.dense2(inputs)
        values = self.values(v1)
        return logits, values

class Worker(threading.Thread):
    # Set up global variables across different threads
    global_episode = 0
    # Moving average reward
    global_moving_average_reward = 0
    best_score = 0
    save_lock = threading.Lock()

    def __init__(self,
            state_size,
            action_size,
            global_model,
            opt,
            result_queue,
            idx,
            game_name='CartPole-v0',
            save_dir='/tmp'):
        super(Worker, self).__init__()
        self.state_size = state_size
        self.action_size = action_size
        self.result_queue = result_queue
        self.global_model = global_model
        self.opt = opt
        self.local_model = ActorCriticModel(self.state_size, self.action_size)
        self.worker_idx = idx
        self.game_name = game_name
        self.env = gym.make(self.game_name).unwrapped
        self.save_dir = save_dir
        self.ep_loss = 0.0

    def run(self):
        print('Worker {} Run'.format(self.worker_idx))
        total_step = 1
        mem = Memory()
        while Worker.global_episode < max_eps:
            current_state = self.env.reset()
            mem.clear()
            ep_reward = 0.0
            ep_steps = 0
            self.ep_loss = 0

            time_count = 0
            done = False
            while not done:
                logits, _ = self.local_model(tf.convert_to_tensor(current_state[None, :], dtype=tf.float32))
                probs = tf.nn.softmax(logits)
                action = np.random.choice(self.action_size, p=probs.numpy()[0]) # numpy() converts tensor to numpy
                new_state, reward, done, _ = self.env.step(action)
                if done:
                    reward = -1
                ep_reward += reward
                mem.store(current_state, action, reward)
                if time_count == update_freq or done:
                    with tf.GradientTape() as tape:
                        total_loss = self.compute_loss(done, new_state, mem, gamma)
                    self.ep_loss += total_loss
                    # Calculate local gradients
                    grads = tape.gradient(total_loss, self.local_model.trainable_weights)
                    # Push local gradients to global model
                    self.opt.apply_gradients(zip(grads, self.global_model.trainable_weights))
                    # Update local model with new weights
                    self.local_model.set_weights(self.global_model.get_weights())

                    mem.clear()
                    time_count = 0

                    if done:  # done and print information
                        Worker.global_moving_average_reward = record(Worker.global_episode, ep_reward, self.worker_idx, Worker.global_moving_average_reward, self.result_queue, self.ep_loss, ep_steps)
                        # We must use a lock to save our model and to print to prevent data races.
                        if ep_reward > Worker.best_score:
                            with Worker.save_lock:
                                print("Saving best model to {}, episode score: {}".format(self.save_dir, ep_reward))
                                self.global_model.save_weights(os.path.join(self.save_dir, 'model_{}.h5'.format(self.game_name)))
                                Worker.best_score = ep_reward
                        Worker.global_episode += 1
                ep_steps += 1

                time_count += 1
                current_state = new_state
                total_step += 1
        self.result_queue.put(None)

    def compute_loss(self, done, new_state, memory, gamma=0.99):
        if done:
            reward_sum = 0.  # terminal
        else:
            reward_sum = self.local_model(tf.convert_to_tensor(new_state[None, :], dtype=tf.float32))[-1].numpy()[0]
        # Get discounted rewards
        discounted_rewards = []
        for reward in memory.rewards[::-1]:  # reverse buffer r
            reward_sum = reward + gamma * reward_sum
            discounted_rewards.append(reward_sum)
        discounted_rewards.reverse()

        logits, values = self.local_model(tf.convert_to_tensor(np.vstack(memory.states), dtype=tf.float32))
        # Get our advantages
        advantage = tf.convert_to_tensor(np.array(discounted_rewards)[:, None], dtype=tf.float32) - values
        # Value loss
        value_loss = advantage ** 2
        # Calculate our policy loss
        policy = tf.nn.softmax(logits)
        entropy = tf.nn.softmax_cross_entropy_with_logits_v2(labels=policy, logits=logits)

        policy_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=memory.actions, logits=logits)
        policy_loss *= tf.stop_gradient(advantage)
        policy_loss -= 0.01 * entropy
        total_loss = tf.reduce_mean((0.5 * value_loss + policy_loss))
        return total_loss


class MasterAgent():

    def __init__(self):
        self.game_name = 'CartPole-v0'
        self.save_dir = save_dir
        env = gym.make(self.game_name)
        self.state_size = env.observation_space.shape[0]
        self.action_size = env.action_space.n
        self.opt = tf.train.AdamOptimizer(learning_rate, use_locking=True)
        print(self.state_size, self.action_size)

        self.global_model = ActorCriticModel(self.state_size, self.action_size)  # global network
        self.global_model(tf.convert_to_tensor(np.random.random((1, self.state_size)), dtype=tf.float32))

    def train(self):
        res_queue = Queue()

        workers = [Worker(self.state_size,
                          self.action_size,
                          self.global_model,
                          self.opt, res_queue,
                          i, game_name=self.game_name,
                          save_dir=self.save_dir) for i in range(multiprocessing.cpu_count())]

        for i, worker in enumerate(workers):
            print("Starting worker {}".format(i))
            worker.start()

        moving_average_rewards = []  # record episode reward to plot
        while True:
            reward = res_queue.get()
            if reward is not None:
                moving_average_rewards.append(reward)
            else:
                break
        [w.join() for w in workers]

        plt.plot(moving_average_rewards)
        plt.ylabel('Moving average ep reward')
        plt.xlabel('Step')
        plt.savefig(os.path.join(self.save_dir, '{} Moving Average.png'.format(self.game_name)))
        plt.show()


master = MasterAgent()
master.train()

'''
global_model = ActorCriticModel(4, 2)  # global network
logits, _ = global_model(tf.convert_to_tensor(np.random.random((1, 4)), dtype=tf.float32))

global_model.trainable_weights

env = gym.make("CartPole-v0")
current_state = env.reset()
assert not (current_state[None, :] - current_state.reshape(-1, current_state.shape[0])).any()

arr = np.array([1,2,3,4])
print(arr.shape)
print(arr[None, :].shape)
print(arr.reshape(-1, arr.shape[0]).shape)

logits = np.array([[0.3256371 , -0.06809683]])
probs = tf.nn.softmax(logits)
probs.numpy().shape


tf.reduce_mean((0.5 * logits))

import tensorflow as tf
tf.enable_eager_execution()
x = tf.contrib.eager.Variable([3.0], dtype=tf.float32)
y = tf.contrib.eager.Variable([4.0], dtype=tf.float32)
with tf.GradientTape() as tape:
    z = x ** 2 + y ** 2
grads = tape.gradient(z, [x, y])
opt = tf.train.GradientDescentOptimizer(learning_rate=0.01)
opt.apply_gradients(zip(grads, [x, y]))
print(grads, x.numpy(), y.numpy())


print(grads)

x = tf.contrib.eager.Variable.Variable(3)
with tf.GradientTape() as g:
    y = x * x
dy = g.gradient(y, x)
print(dy)
'''


a = [1,2,3]
ass = []
ass.append(a)
ass.append(a)
ass.append(a)
np.vstack(ass)

assert not (np.vstack(ass)- np.array(ass)).any()

logits = np.array([1.0, -1.0])
policy = tf.nn.softmax(logits)
entropy = tf.nn.softmax_cross_entropy_with_logits_v2(labels=policy, logits=logits)
print(policy, entropy)
