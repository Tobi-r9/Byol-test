import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import argparse
import numpy as np
import tensorflow as tf

from datasets import CIFAR10
from models import ResNet18, ResNet34, ProjectionHead
from losses import byol_loss



encoders = {'resnet18': ResNet18, 'resnet34': ResNet34}

def update_f(F, corr, lambda_=0.8):
    if F is None:
        F = corr
    else:
        F = lambda_ * F + (1 - lambda_) * corr
    return F

def main(epochs=30, num_samples=30000):

    # eigenspace allignment
    F = None
    F_eigenval = []
    allignment = []
    wp_eigenval = []
    cosine_sim = tf.keras.losses.CosineSimilarity(
        axis=0,
        reduction=tf.keras.losses.Reduction.NONE
        )

    # Load CIFAR-10 dataset
    data = CIFAR10(num_samples)

    # Instantiate networks
    f_online = encoders['resnet18']()
    g_online = ProjectionHead()
    q_online = ProjectionHead()

    f_target = encoders['resnet18']()
    g_target = ProjectionHead()


    # Initialize the weights of the networks
    x = tf.random.normal((256, 32, 32, 3))
    h = f_online(x, training=False)
    print('Initializing online networks...')
    print('Shape of h:', h.shape)
    z = g_online(h, training=False)
    print('Shape of z:', z.shape)
    p = q_online(z, training=False)
    print('Shape of p:', p.shape)

    h = f_target(x, training=False)
    print('Initializing target networks...')
    print('Shape of h:', h.shape)
    z = g_target(h, training=False)
    print('Shape of z:', z.shape)
    
    num_params_f = tf.reduce_sum([tf.reduce_prod(var.shape) for var in f_online.trainable_variables])    
    print('The encoders have {} trainable parameters each.'.format(num_params_f))


    # Define optimizer
    lr = 1e-3 * 512 / 512
    opt = tf.keras.optimizers.Adam(learning_rate=lr)
    print('Using Adam optimizer with learning rate {}.'.format(lr))


    @tf.function
    def train_step_pretraining(x1, x2, F):  # (bs, 32, 32, 3), (bs, 32, 32, 3)

        # Forward pass
        h_target_1 = f_target(x1, training=True)
        z_target_1 = g_target(h_target_1, training=True)

        h_target_2 = f_target(x2, training=True)
        z_target_2 = g_target(h_target_2, training=True)

        with tf.GradientTape(persistent=True) as tape:
            h_online_1 = f_online(x1, training=True)
            z_online_1 = g_online(h_online_1, training=True)
            p_online_1 = q_online(z_online_1, training=True)
            
            h_online_2 = f_online(x2, training=True)
            z_online_2 = g_online(h_online_2, training=True)
            p_online_2 = q_online(z_online_2, training=True)
            
            p_online = tf.concat([p_online_1, p_online_2], axis=0)
            z_target = tf.concat([z_target_2, z_target_1], axis=0)
            loss = byol_loss(p_online, z_target)

        # Backward pass (update online networks)
        grads = tape.gradient(loss, f_online.trainable_variables)
        opt.apply_gradients(zip(grads, f_online.trainable_variables))
        grads = tape.gradient(loss, g_online.trainable_variables)
        opt.apply_gradients(zip(grads, g_online.trainable_variables))
        grads = tape.gradient(loss, q_online.trainable_variables)
        opt.apply_gradients(zip(grads, q_online.trainable_variables))
        del tape
        
        corr_1 = tf.matmul(tf.expand_dims(z_online_1, 2), tf.expand_dims(z_online_1, 1))
        corr_2 = tf.matmul(tf.expand_dims(z_online_2, 2), tf.expand_dims(z_online_2, 1))
        corr = tf.concat([corr_1, corr_2], axis=0)
        corr = tf.reduce_mean(corr, axis=0)
        F = update_f(F, corr)
        

        return loss, F


    batches_per_epoch = data.num_train_images // 512
    log_every = 10  # batches
    save_every = 100  # epochs

    losses = []
    for epoch_id in range(epochs):
        data.shuffle_training_data()
        
        for batch_id in range(batches_per_epoch):
            x1, x2 = data.get_batch_pretraining(batch_id, 512)
            loss, F = train_step_pretraining(x1, x2, F)
            losses.append(float(loss))

            # Update target networks (exponential moving average of online networks)
            beta = 0.99

            f_target_weights = f_target.get_weights()
            f_online_weights = f_online.get_weights()
            for i in range(len(f_online_weights)):
                f_target_weights[i] = beta * f_target_weights[i] + (1 - beta) * f_online_weights[i]
            f_target.set_weights(f_target_weights)
            
            g_target_weights = g_target.get_weights()
            g_online_weights = g_online.get_weights()
            for i in range(len(g_online_weights)):
                g_target_weights[i] = beta * g_target_weights[i] + (1 - beta) * g_online_weights[i]
            g_target.set_weights(g_target_weights)

            if (batch_id + 1) % log_every == 0:
                print('[Epoch {}/{} Batch {}/{}] Loss={:.5f}.'.format(epoch_id+1, epochs, batch_id+1, batches_per_epoch, loss))

        if (epoch_id + 1) % save_every == 0:
            f_online.save_weights('f_online_{}.h5'.format(epoch_id + 1))
            print('Weights of f saved.')

        if epoch_id % 5 == 0:    
            # get F eignvalues
            eigval, eigvec = tf.linalg.eigh(F)
            F_eigenval.append(eigval)

            # get predictor head
            w1 = q_online.fc1.get_weights()[0]
            w2 = q_online.fc2.get_weights()[0]
            wp = tf.matmul(w1,w2)
            wp = tf.transpose(wp)
            wp_eigval = tf.linalg.eigvals(wp)
            wp_eigval = tf.math.real(wp_eigval)
            wp_eigenval.append(wp_eigval)

            wp_v = tf.matmul(wp, eigvec)
            cosine = cosine_sim(eigvec, wp_v)
            allignment.append(cosine)
    
    np.savetxt('losses.txt', tf.stack(losses).numpy())

    return F_eigenval, allignment, wp_eigenval
