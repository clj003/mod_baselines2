import numpy as np
import tensorflow as tf
from baselines import logger
import baselines.common as common
from baselines.common import tf_util as U
from baselines.old_acktr import kfac
from baselines.common.filters import ZFilter

import os

import random


def pathlength(path):
    return path["reward"].shape[0]# Loss function that we'll differentiate to get the policy gradient

def rollout(env, policy, max_pathlength, last_ob_array, last_jpos_array, animate=False, obfilter=None):
    """
    Simulate the env and policy for max_pathlength steps
    """
    ob = env.reset()

    # To account for previous policy run
    #start_inds = random.randint(3450,last_ob_array.shape[0]-1) # start from 2600 for now by inspection, change to make this less hacky later
    #env.set_robot_joint_positions(last_jpos_array[start_inds])
    #ob = last_ob_array[start_inds]

    #env.set_robot_joint_positions(last_jpos_array[-1]) # last position, comment out for now for move_lift_grip, otherwise dont for grasp and lift, also commented out to show acktr works alone
    
    #ob = np.concatenate((last_ob_array[-1], env.box_end),axis=0) # comment out to show acktr works alone without the gail traj

    

    prev_ob = np.float32(np.zeros(ob.shape))
    if obfilter: ob = obfilter(ob)
    terminated = False

    obs = []
    acs = []
    ac_dists = []
    logps = []
    rewards = []
    for _ in range(max_pathlength):
        if animate:
            env.render()
        state = np.concatenate([ob, prev_ob], -1)
        obs.append(state)
        ac, ac_dist, logp = policy.act(state)
        acs.append(ac)
        ac_dists.append(ac_dist)
        logps.append(logp)
        prev_ob = np.copy(ob)
        scaled_ac = env.action_space.low + (ac + 1.) * 0.5 * (env.action_space.high - env.action_space.low)
        scaled_ac = np.clip(scaled_ac, env.action_space.low, env.action_space.high)
        ob, rew, done, _ = env.step(scaled_ac)
        if obfilter: ob = obfilter(ob)
        rewards.append(rew)
        if done:
            terminated = True
            break
    return {"observation" : np.array(obs), "terminated" : terminated,
            "reward" : np.array(rewards), "action" : np.array(acs),
            "action_dist": np.array(ac_dists), "logp" : np.array(logps)}

def old_acktr_learn(env, policy, vf, gamma, lam, timesteps_per_batch, num_timesteps, save_per_iter, ckpt_dir, max_dir, traj_limitation, last_ob, last_jpos, 
    animate=False, callback=None, desired_kl=0.002, cont_training=False,load_path=None, obfilter_load=None, pi_name="pi_aktr" ):

    obfilter = ZFilter(env.observation_space.shape)

    # Set a number for episode mean max
    max_mean = float('-inf')

    #max_pathlength = env.spec.timestep_limit
    max_pathlength = traj_limitation

    stepsize = tf.Variable(initial_value=np.float32(np.array(0.03)), name='stepsize')
    inputs, loss, loss_sampled = policy.update_info
    optim = kfac.KfacOptimizer(learning_rate=stepsize, cold_lr=stepsize*(1-0.9), momentum=0.9, kfac_update=2,\
                                epsilon=1e-2, stats_decay=0.99, async_=1, cold_iter=1,
                                weight_decay_dict=policy.wd_dict, max_grad_norm=None)
    pi_var_list = []
    for var in tf.trainable_variables():
        if pi_name in var.name:
            pi_var_list.append(var)

    update_op, q_runner = optim.minimize(loss, loss_sampled, var_list=pi_var_list)
    do_update = U.function(inputs, update_op)
    U.initialize()

    # Loading operation
    if cont_training:
        sess = tf.compat.v1.get_default_session()
        saver_cont = tf.compat.v1.train.Saver(max_to_keep=5)
        ckpt_cont = tf.compat.v1.train.get_checkpoint_state(load_path)
        saver_cont.restore(sess,ckpt_cont.model_checkpoint_path)

        stat_cont = np.load(obfilter_load)
        obfilter.rs._n = stat_cont["n"]
        obfilter.rs._M = stat_cont["M"]
        obfilter.rs._S = stat_cont["S"]
    
    
    # start queue runners
    enqueue_threads = []
    coord = tf.train.Coordinator()
    for qr in [q_runner, vf.q_runner]:
        assert (qr != None)
        enqueue_threads.extend(qr.create_threads(tf.compat.v1.get_default_session(), coord=coord, start=True))

    i = 0
    timesteps_so_far = 0
    while True:
        if timesteps_so_far > num_timesteps:
            break

        

        logger.log("********** Iteration %i ************"%i)

        # Collect paths until we have enough timesteps
        timesteps_this_batch = 0
        paths = []
        while True:
            path = rollout(env, policy, max_pathlength, last_ob, last_jpos, animate=(len(paths)==0 and (i % 30 == 0) and animate), obfilter=obfilter)
            paths.append(path)
            n = pathlength(path)
            timesteps_this_batch += n
            timesteps_so_far += n
            if timesteps_this_batch > timesteps_per_batch:
                break

        # Estimate advantage function
        vtargs = []
        advs = []
        for path in paths:
            rew_t = path["reward"]
            return_t = common.discount(rew_t, gamma)
            vtargs.append(return_t)
            vpred_t = vf.predict(path)
            vpred_t = np.append(vpred_t, 0.0 if path["terminated"] else vpred_t[-1])
            delta_t = rew_t + gamma*vpred_t[1:] - vpred_t[:-1]
            adv_t = common.discount(delta_t, gamma * lam)
            advs.append(adv_t)
        # Update value function
        vf.fit(paths, vtargs)

        # Build arrays for policy update
        ob_no = np.concatenate([path["observation"] for path in paths])
        action_na = np.concatenate([path["action"] for path in paths])
        oldac_dist = np.concatenate([path["action_dist"] for path in paths])
        adv_n = np.concatenate(advs)
        standardized_adv_n = (adv_n - adv_n.mean()) / (adv_n.std() + 1e-8)

        # Policy update
        do_update(ob_no, action_na, standardized_adv_n)
        
        
        # Add in for saving weights
        if (i % save_per_iter == 0) and ckpt_dir is not None:

            logger.log("Saving weights")

            task_name = "acktr_weights"+ "_" + str(timesteps_so_far)
            fname = os.path.join( ckpt_dir ,task_name)
            saver = tf.compat.v1.train.Saver(max_to_keep=5)
            saver.save(tf.compat.v1.get_default_session(), fname)

            ob_filter_file_name = "filter_stats"+"_" + str(timesteps_so_far)
            ob_filter_fname = os.path.join(ckpt_dir , ob_filter_file_name)

            np.savez(
                    ob_filter_fname,
                    n=obfilter.rs._n,
                    M=obfilter.rs._M,
                    S=obfilter.rs._S,
                    )

            logger.log("ob_filter stats saved")



        now_mean = np.mean([path["reward"].sum() for path in paths])

        if ( now_mean > max_mean ):
            max_mean = now_mean
            logger.log("max_mean updated: ", max_mean)
            
            logger.log("Saving max  weights")

            max_task_name = "acktr_weights"+ "_max" + "_" + str(timesteps_so_far)
            max_fname = os.path.join( max_dir , max_task_name)
            max_saver = tf.compat.v1.train.Saver(max_to_keep=5)
            max_saver.save(tf.compat.v1.get_default_session(), max_fname)

            max_ob_filter_file_name = "filter_stats"+"_max" + "_" + str(timesteps_so_far)
            max_ob_filter_fname = os.path.join(max_dir , max_ob_filter_file_name)

            np.savez(
                    max_ob_filter_fname,
                    n=obfilter.rs._n,
                    M=obfilter.rs._M,
                    S=obfilter.rs._S,
                    )

        
        min_stepsize = np.float32(1e-8)
        max_stepsize = np.float32(1e0)
        # Adjust stepsize
        kl = policy.compute_kl(ob_no, oldac_dist)
        if kl > desired_kl * 2:
            logger.log("kl too high")
            tf.assign(stepsize, tf.maximum(min_stepsize, stepsize / 1.5)).eval()
        elif kl < desired_kl / 2:
            logger.log("kl too low")
            tf.assign(stepsize, tf.minimum(max_stepsize, stepsize * 1.5)).eval()
        else:
            logger.log("kl just right!")

        logger.record_tabular("EpRewMean", np.mean([path["reward"].sum() for path in paths]))
        logger.record_tabular("EpRewSEM", np.std([path["reward"].sum()/np.sqrt(len(paths)) for path in paths]))
        logger.record_tabular("EpLenMean", np.mean([pathlength(path) for path in paths]))
        logger.record_tabular("KL", kl)
        if callback:
            callback()
        logger.dump_tabular()
        i += 1

    coord.request_stop()
    coord.join(enqueue_threads)
