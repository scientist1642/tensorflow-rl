import sys
import gym
import time
from multiprocessing import Process


def run_env(pname, n_steps):
    env = gym.make('Pong-v0')
    env.reset()
    start_time = time.time()
    for _ in range(n_steps):
        #env.render()
        action = env.action_space.sample()
        observation, reward, done, info = env.step(action)
        ob = env.step(env.action_space.sample()) # take a random action
        if done:
            env.reset()

    print ('Process {} finished in {} sec'.format(pname, time.time() - start_time)) 
    env.close()

if __name__=='__main__':

    n_workers = int(sys.argv[1]) # number of parallel workers
    n_steps = 10000
    print ('Running {} processes of env each with {} steps'.format(n_workers, n_steps))
    start_time = time.time()
    procs = []
    for i in range(n_workers):
        p = Process(target=run_env, args=(i, n_steps))
        procs.append(p)
        p.start()

    for p in procs:
        p.join()
    print ('Everything finished in {} sec'.format(time.time() - start_time)) 
