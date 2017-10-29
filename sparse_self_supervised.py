from __future__ import print_function

import tensorflow as tf
from base import Model
from util import get_data
from sparse_util import *
import math
import time
from tqdm import tqdm
from collections import OrderedDict

def sample_submatrix(mask_,#mask, used for getting concentrations
                     maxN, maxM,
                     sample_uniform=False):
    '''
    sampling mini-batches. Currently it is sampling rows and columns based on the number of non-zeros.
    In the sparse implementation, we could sample non-zero entries and directly.
    '''
    pN, pM = mask_.sum(axis=1)[:,0], mask_.sum(axis=0)[:,0]
    pN /= pN.sum()#non-zero dist per row
    pM /= pM.sum()#non-zero dist per column
    
    N, M, _ = mask_.shape
    for n in range(N // maxN):
        for m in range(M // maxM):
            if N == maxN:
                ind_n = np.arange(N)
            else:
                ind_n = np.random.choice(N, size=maxN, replace=False, p = pN)#select a row based on density of nonzeros
            if M == maxM:
                ind_m = np.arange(M)
            else:
                ind_m = np.random.choice(M, size=maxM, replace=False, p = pM)
            yield ind_n, ind_m 


def sample_dense_values_uniform(mask_indices, minibatch_size, iters_per_epoch):
    num_vals = mask_indices.shape[0]
    minibatch_size = np.minimum(minibatch_size, num_vals)
    for n in range(iters_per_epoch):
        sample = np.random.choice(num_vals, size=minibatch_size, replace=False)
        yield np.sort(sample)

def rec_loss_fn_sp(mat_values, mask_indices, rec_values, mask_split):
    return tf.reduce_sum((mat_values - rec_values)**2 * mask_split) / tf.cast(tf.shape(mask_indices)[0], tf.float32)

def dae_loss_fn_sp(mat_values, rec_values, noise_mask, alpha):
    noise_mask = tf.cast(noise_mask, tf.float32)
    no_noise_mask = tf.ones_like(noise_mask) - noise_mask
    diff = (mat_values - rec_values)**2
    diff_c = diff * noise_mask
    diff_u = diff * no_noise_mask
    loss_c = tf.reduce_sum(diff_c)
    loss_u = tf.reduce_sum(diff_u)
    eps = 1e-10
    return alpha * loss_c / (tf.reduce_sum(noise_mask) + eps) + (1-alpha) * loss_u / (tf.reduce_sum(no_noise_mask) + eps)


def main(opts):        
    gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.9)    
    path = opts['data_path']
    data = get_data(path, train=.8, valid=.1, test=.1)
    
    #build encoder and decoder and use VAE loss
    N, M, num_features = data['mat_shape']
    maxN, maxM = opts['maxN'], opts['maxM']

    if N < maxN: maxN = N
    if M < maxM: maxM = M

    if opts['verbose'] > 0:
        print('\nFactorized Autoencoder run settings:')
        print('dataset: ', path)
        print('Exchangable layer pool mode: ', opts['defaults']['matrix_sparse']['pool_mode'])
        print('learning rate: ', opts['lr'])
        print('activation: ', opts['defaults']['matrix_sparse']['activation'])
        print('maxN: ', opts['maxN'])
        print('maxM: ', opts['maxM'])
        print('')

    with tf.Graph().as_default():
        # with tf.device('/gpu:0'):
            mat_values_tr = tf.placeholder(tf.float32, shape=[None], name='mat_values_tr')
            mat_values_tr_noisy = tf.placeholder(tf.float32, shape=[None], name='mat_values_tr_noisy')
            mask_indices_tr = tf.placeholder(tf.int64, shape=[None, 2], name='mask_indices_tr')
            mat_shape_tr = tf.placeholder(tf.int32, shape=[3], name='mat_shape_tr')

            mat_values_val = tf.placeholder(tf.float32, shape=[None], name='mat_values_val')
            mat_values_val_noisy = tf.placeholder(tf.float32, shape=[None], name='mat_values_val_noisy')
            mask_indices_val = tf.placeholder(tf.int64, shape=[None, 2], name='mask_indices_val')
            mat_shape_val = tf.placeholder(tf.int32, shape=[3], name='mat_shape_val')

            noise_mask_tr = tf.placeholder(tf.int64, shape=(None), name='noise_mask_tr')
            noise_mask_val = tf.placeholder(tf.int64, shape=(None), name='noise_mask_val')
            

            with tf.variable_scope("network"):
                tr_dict = {'input':mat_values_tr_noisy,
                           'mask_indices':mask_indices_tr,
                           'shape':mat_shape_tr,
                           'units':1}

                val_dict = {'input':mat_values_val_noisy,
                            'mask_indices':mask_indices_val,
                            'shape':mat_shape_val,
                            'units':1}

                network = Model(layers=opts['network'], layer_defaults=opts['defaults'], verbose=2) #define the network
                out_tr = network.get_output(tr_dict)['input'] #build the network
                out_val = network.get_output(val_dict, reuse=True, verbose=0, is_training=False)['input']#get network output, reusing the neural net
            

            #loss and training
            # rec_loss = rec_loss_fn_sp(mat_values_tr, mask_indices_tr, out_tr, tf.ones(tf.shape(mat_values_tr)))
            rec_loss = dae_loss_fn_sp(mat_values_tr, out_tr, noise_mask_tr, opts['dae_loss_alpha'])
            reg_loss = sum(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)) # regularization            
            
            rec_loss_val = dae_loss_fn_sp(mat_values_val, out_val, noise_mask_val, 1)
            
            total_loss = rec_loss + reg_loss

            train_step = tf.train.AdamOptimizer(opts['lr']).minimize(total_loss)
            sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))
            sess.run(tf.global_variables_initializer())

            if 'by_row_column_density' in opts['sample_mode']:
                iters_per_epoch = math.ceil(N//maxN) * math.ceil(M//maxM) # a bad heuristic: the whole matrix is in expectation covered in each epoch
            elif 'uniform_over_dense_values' in opts['sample_mode']:
                minibatch_size = np.minimum(opts['minibatch_size'], data['mask_indices_tr'].shape[0])
                iters_per_epoch = data['mask_indices_tr'].shape[0] // minibatch_size
            
            min_loss = 5
            min_loss_epoch = 0
            losses = OrderedDict()
            losses["train"] = []
            losses["valid"] = []

            noise_rate = opts['dae_noise_rate']
            
            for ep in range(opts['epochs']):
                begin = time.time()
                loss_tr_, rec_loss_tr_, loss_val_, loss_ts_ = 0,0,0,0

                if 'by_row_column_density' in opts['sample_mode']:
                    for indn_, indm_ in tqdm(sample_submatrix(data['mask_tr'], maxN, maxM, sample_uniform=False), total=iters_per_epoch):#go over mini-batches
                        
                        inds_ = np.ix_(indn_,indm_,[0])#select a sub-matrix given random indices for users/movies                    
                        mat_sp = data['mat_tr_val'][inds_] * data['mask_tr'][inds_]
                        mat_values = dense_array_to_sparse(mat_sp)['values']
                        mask_indices = dense_array_to_sparse(data['mask_tr'][inds_])['indices'][:,0:2]           

                        noise_mask = np.random.choice([0,1], size=mask_indices.shape[0], p=[1-noise_rate, noise_rate]) # which entries to 'corrupt' by dropping out 
                        no_noise_mask = np.ones_like(noise_mask) - noise_mask
                        mat_values_noisy = mat_values * no_noise_mask
                        
                        tr_dict = {mat_values_tr:mat_values,
                                    mat_values_tr_noisy:mat_values_noisy,
                                    mask_indices_tr:mask_indices,
                                    noise_mask_tr:noise_mask,
                                    mat_shape_tr:[maxN,maxM,1]}

                    _, bloss_, brec_loss_ = sess.run([train_step, total_loss, rec_loss], feed_dict=tr_dict)

                    loss_tr_ += np.sqrt(bloss_)
                    rec_loss_tr_ += np.sqrt(brec_loss_)
                                
                elif 'uniform_over_dense_values' in opts['sample_mode']:                    
                    for sample_ in tqdm(sample_dense_values_uniform(data['mask_indices_tr'], minibatch_size, iters_per_epoch), total=iters_per_epoch):

                        mat_values = data['mat_values_tr'][sample_]
                        mask_indices = data['mask_indices_tr'][sample_]
                        batchN = mask_indices[minibatch_size-1,0] + 1
                        batchM = np.max(mask_indices[:,1]) + 1

                        noise_mask = np.random.choice([0,1], size=mask_indices.shape[0], p=[1-noise_rate, noise_rate]) # which entries to 'corrupt' by dropping out 
                        no_noise_mask = np.ones_like(noise_mask) - noise_mask
                        mat_values_noisy = mat_values * no_noise_mask
                        
                        tr_dict = {mat_values_tr:mat_values,
                                    mat_values_tr_noisy:mat_values_noisy,
                                    mask_indices_tr:mask_indices,
                                    noise_mask_tr:noise_mask,
                                    mat_shape_tr:[batchN,batchM,1]}

                        _, bloss_, brec_loss_ = sess.run([train_step, total_loss, rec_loss], feed_dict=tr_dict)

                        loss_tr_ += np.sqrt(bloss_)
                        rec_loss_tr_ += np.sqrt(brec_loss_)



                else:
                    print('\nERROR - unknown <sample_mode> in main()\n')
                    return


                loss_tr_ /= iters_per_epoch
                rec_loss_tr_ /= iters_per_epoch

                ## Validation Loss
                # noise_mask = np.random.choice([0,1], size=data['mask_indices_val'].shape[0], p=[1-noise_rate, noise_rate]) # which entries to 'corrupt' by dropping out 
                noise_mask = data['mask_tr_val_split'] # 1's for validation entries 
                no_noise_mask = np.ones_like(noise_mask) - noise_mask
                mat_values_noisy = data['mat_values_tr_val'] * no_noise_mask

                val_dict = {mat_values_val:data['mat_values_tr_val'],
                            mat_values_val_noisy:mat_values_noisy,
                            mask_indices_val:data['mask_indices_tr_val'],
                            noise_mask_val:noise_mask,
                            mat_shape_val:[N,M,1]}

                bloss_, = sess.run([rec_loss_val], feed_dict=val_dict)


                loss_val_ += np.sqrt(bloss_)
                if loss_val_ < min_loss: # keep track of the best validation loss 
                    min_loss = loss_val_
                    min_loss_epoch = ep
                losses['train'].append(loss_tr_)
                losses['valid'].append(loss_val_)

                print("epoch {:d} took {:.1f} training loss {:.3f} (rec:{:.3f}) \t validation: {:.3f} \t minimum validation loss: {:.3f} at epoch: {:d} \t test loss: {:.3f}".format(ep, time.time() - begin, loss_tr_, rec_loss_tr_, loss_val_, min_loss, min_loss_epoch, loss_ts_))            
    return losses    

if __name__ == "__main__":

    # path = 'movielens-TEST'
    path = 'movielens-100k'
    # path = 'movielens-1M'
    # path = 'netflix/6m'    

    ## 100k Configs
    if 'movielens-100k' in path:
        maxN = 10000
        maxM = 10000
        minibatch_size = 5000000
        skip_connections = True
        units = 32
        learning_rate = 0.001
        dae_noise_rate = .5 # drop out this proportion of input values 
        dae_loss_alpha = .5  # proportion of loss assigned to predicting droped out values 


    ## 1M Configs
    if 'movielens-1M' in path:
        maxN = 1000
        maxM = 1000
        minibatch_size = 1000
        skip_connections = True
        units = 32
        learning_rate = 0.001
        dae_noise_rate = .1 # drop out this proportion of input values 
        dae_loss_alpha = .6  # proportion of loss assigned to predicting droped out values 

    if 'netflix/6m' in path:
        maxN = 300
        maxM = 300
        minibatch_size = 1000
        skip_connections = True
        units = 32
        learning_rate = 0.001


    opts ={'epochs': 1000,#never-mind this. We have to implement look-ahead to report the best result.
           'ckpt_folder':'checkpoints/factorized_ae',
           'model_name':'test_fac_ae',
           'verbose':2,
           # 'maxN':943,#num of users per submatrix/mini-batch, if it is the total users, no subsampling will be performed
           # 'maxM':1682,#num movies per submatrix
           'maxN':maxN,#num of users per submatrix/mini-batch, if it is the total users, no subsampling will be performed
           'maxM':maxM,#num movies per submatrix
           'minibatch_size':minibatch_size,
           'visualize':False,
           'save':False,
           'data_path':path,
           'output_file':'output',
           'network':[
               {'type':'matrix_sparse', 'units':units},
               # {'type':'matrix_dropout_sparse'},
               {'type':'matrix_sparse', 'units':units, 'skip_connections':skip_connections},
               # {'type':'matrix_dropout_sparse'},
               {'type':'matrix_sparse', 'units':1, 'activation':None},#units before matrix-pool is the number of latent features for each movie and each user in the factorization
               ],
            'defaults':{#default values for each layer type (see layer.py)
                'matrix_sparse':{
                    # 'activation':tf.nn.tanh,
                    # 'activation':tf.nn.sigmoid,
                    'activation':tf.nn.relu,
                    # 'drop_mask':False,#whether to go over the whole matrix, or emulate the sparse matrix in layers beyond the input. If the mask is droped the whole matrix is used.
                    'pool_mode':'max',#mean vs max in the exchangeable layer. Currently, when the mask is present, only mean is supported
                    'kernel_initializer': tf.random_normal_initializer(0, .01),
                    'regularizer': tf.contrib.keras.regularizers.l2(.00001),
                    'skip_connections':False,
                },
                'dense':{#not used
                    'activation':tf.nn.elu,
                    'kernel_initializer': tf.random_normal_initializer(0, .01),
                    'regularizer': tf.contrib.keras.regularizers.l2(.00001),
                },                
                'matrix_dropout_sparse':{
                    'rate':.2,
                }
            },
           'lr':learning_rate,
           'sample_mode':'uniform_over_dense_values', # by_row_column_density, uniform_over_dense_values
           'dae_noise_rate':dae_noise_rate,
           'dae_loss_alpha':dae_loss_alpha,
    }
    
    main(opts)

