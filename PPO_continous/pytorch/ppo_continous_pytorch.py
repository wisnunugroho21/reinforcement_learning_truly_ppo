import gym
from gym.envs.registration import register
    
import torch
import torch.nn as nn
from torch.distributions import Normal
from torch.distributions.kl import kl_divergence
from torch.utils.data import Dataset, DataLoader
from torch.optim import Adam
from torch.utils.tensorboard import SummaryWriter

import numpy as np
import sys
import numpy
import time
import datetime

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")  
dataType = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor

class Actor_Model(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Actor_Model, self).__init__()   

        self.nn_layer = nn.Sequential(
                nn.Linear(state_dim, 256),
                nn.ReLU(),
                nn.Linear(256, 64),
                nn.ReLU(),
                nn.Linear(64, action_dim),
                nn.Tanh()
              ).float().to(device)
        
    def forward(self, states):
        return self.nn_layer(states)

class Critic_Model(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Critic_Model, self).__init__()   

        self.nn_layer = nn.Sequential(
                nn.Linear(state_dim, 256),
                nn.ReLU(),
                nn.Linear(256, 64),
                nn.ReLU(),
                nn.Linear(64, 1)
              ).float().to(device)
        
    def forward(self, states):
        return self.nn_layer(states)

class Memory(Dataset):
    def __init__(self):
        self.actions        = [] 
        self.states         = []
        self.rewards        = []
        self.dones          = []     
        self.next_states    = []

    def __len__(self):
        return len(self.dones)

    def __getitem__(self, idx):
        return np.array(self.states[idx], dtype = np.float32), np.array(self.actions[idx], dtype = np.float32), np.array([self.rewards[idx]], dtype = np.float32), np.array([self.dones[idx]], dtype = np.float32), np.array(self.next_states[idx], dtype = np.float32)      

    def save_eps(self, state, action, reward, done, next_state):
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.dones.append(done)
        self.next_states.append(next_state)     

    def clear_memory(self):
        del self.actions[:]
        del self.states[:]
        del self.rewards[:]
        del self.dones[:]
        del self.next_states[:]  

class Continous():
    def sample(self, mean, std):
        distribution    = Normal(mean, std)
        return distribution.sample().float().to(device)
        
    def entropy(self, mean, std):
        distribution    = Normal(mean, std)    
        return distribution.entropy().float().to(device)
      
    def logprob(self, mean, std, value_data):
        distribution    = Normal(mean, std)
        return distribution.log_prob(value_data).float().to(device)

    def kl_divergence(self, mean1, std1, mean2, std2):
        distribution1   = Normal(mean1, std1)
        distribution2   = Normal(mean2, std2)

        return kl_divergence(distribution1, distribution2).float().to(device)  

class PolicyFunction():
    def __init__(self, gamma = 0.99, lam = 0.95):
        self.gamma  = gamma
        self.lam    = lam

    def monte_carlo_discounted(self, rewards, dones):
        running_add = 0
        returns     = []        
        
        for step in reversed(range(len(rewards))):
            running_add = rewards[step] + (1.0 - dones[step]) * self.gamma * running_add
            returns.insert(0, running_add)
            
        return torch.stack(returns)
      
    def temporal_difference(self, reward, next_value, done):
        q_values = reward + (1 - done) * self.gamma * next_value           
        return q_values
      
    def generalized_advantage_estimation(self, values, rewards, next_values, dones):
        gae     = 0
        adv     = []     

        delta   = rewards + (1.0 - dones) * self.gamma * next_values - values          
        for step in reversed(range(len(rewards))):  
            gae = delta[step] + (1.0 - dones[step]) * self.gamma * self.lam * gae
            adv.insert(0, gae)
            
        return torch.stack(adv)

class TrulyPPO():
    def __init__(self, policy_kl_range, policy_params, value_clip, vf_loss_coef, entropy_coef, gamma, lam):
        self.policy_kl_range    = policy_kl_range
        self.policy_params      = policy_params
        self.value_clip         = value_clip
        self.vf_loss_coef       = vf_loss_coef
        self.entropy_coef       = entropy_coef

        self.distributions      = Continous()
        self.policy_function    = PolicyFunction(gamma, lam)

    def compute_loss(self, action_mean, action_std, old_action_mean, old_action_std, values, old_values, next_values, actions, rewards, dones):    
        # Don't use old value in backpropagation
        Old_values          = old_values.detach()
        Old_action_mean     = old_action_mean.detach()

        # Getting general advantages estimator and returns
        Advantages      = self.policy_function.generalized_advantage_estimation(values, rewards, next_values, dones)
        Returns         = (Advantages + values).detach()
        Advantages      = ((Advantages - Advantages.mean()) / (Advantages.std() + 1e-6)).detach() 

        # Finding the ratio (pi_theta / pi_theta__old):  
        logprobs        = self.distributions.logprob(action_mean, action_std, actions)
        Old_logprobs    = self.distributions.logprob(Old_action_mean, old_action_std, actions).detach() 

        # Finding Surrogate Loss
        ratios          = (logprobs - Old_logprobs).exp() # ratios = old_logprobs / logprobs        
        Kl              = self.distributions.kl_divergence(Old_action_mean, old_action_std, action_mean, action_std)

        pg_targets  = torch.where(
            (Kl >= self.policy_kl_range) & (ratios > 1),
            ratios * Advantages - self.policy_params * Kl,
            ratios * Advantages
        )
        pg_loss     = pg_targets.mean()

        # Getting entropy from the action probability 
        dist_entropy    = self.distributions.entropy(action_mean, action_std).mean()

        # Getting Critic loss by using Clipped critic value
        if self.value_clip is None:
            critic_loss   = ((Returns - values).pow(2) * 0.5).mean()
        else:
            vpredclipped  = old_values + torch.clamp(values - Old_values, -self.value_clip, self.value_clip) # Minimize the difference between old value and new value
            vf_losses1    = (Returns - values).pow(2) * 0.5 # Mean Squared Error
            vf_losses2    = (Returns - vpredclipped).pow(2) * 0.5 # Mean Squared Error        
            critic_loss   = torch.max(vf_losses1, vf_losses2).mean()                

        # We need to maximaze Policy Loss to make agent always find Better Rewards
        # and minimize Critic Loss 
        loss = (critic_loss * self.vf_loss_coef) - (dist_entropy * self.entropy_coef) - pg_loss
        return loss

class Agent():  
    def __init__(self, state_dim, action_dim, is_training_mode, policy_kl_range, policy_params, value_clip, entropy_coef, vf_loss_coef,
                 minibatch, PPO_epochs, gamma, lam, learning_rate):        
        self.policy_kl_range    = policy_kl_range 
        self.policy_params      = policy_params
        self.value_clip         = value_clip    
        self.entropy_coef       = entropy_coef
        self.vf_loss_coef       = vf_loss_coef
        self.minibatch          = minibatch       
        self.PPO_epochs         = PPO_epochs
        self.is_training_mode   = is_training_mode
        self.action_dim         = action_dim 
        self.std                = torch.ones([1, action_dim]).float().to(device)                

        self.actor              = Actor_Model(state_dim, action_dim)
        self.actor_old          = Actor_Model(state_dim, action_dim)
        self.actor_optimizer    = Adam(self.actor.parameters(), lr = learning_rate)

        self.critic             = Critic_Model(state_dim, action_dim)
        self.critic_old         = Critic_Model(state_dim, action_dim)
        self.critic_optimizer   = Adam(self.critic.parameters(), lr = learning_rate)

        self.memory             = Memory()
        self.policy_function    = PolicyFunction(gamma, lam)

        self.distributions      = Continous()
        self.policy_loss        = TrulyPPO(policy_kl_range, policy_params, value_clip, vf_loss_coef, entropy_coef, gamma, lam)

        if is_training_mode:
          self.actor.train()
          self.critic.train()
        else:
          self.actor.eval()
          self.critic.eval()

    def save_eps(self, state, action, reward, done, next_state):
        self.memory.save_eps(state, action, reward, done, next_state)

    def act(self, state):
        state       = torch.FloatTensor(state).unsqueeze(0).to(device).detach()
        action_mean = self.actor(state)
        
        # We don't need sample the action in Test Mode
        # only sampling the action in Training Mode in order to exploring the actions
        if self.is_training_mode:
            # Sample the action
            action  = self.distributions.sample(action_mean, self.std)
        else:
            action  = action_mean  
              
        return action.squeeze(0).cpu().numpy()

    # Get loss and Do backpropagation
    def training_ppo(self, states, actions, rewards, dones, next_states):                 
        action_mean, values          = self.actor(states), self.critic(states)
        old_action_mean, old_values  = self.actor_old(states), self.critic_old(states)
        next_values                  = self.critic(next_states)
  
        loss    = self.policy_loss.compute_loss(action_mean, self.std, old_action_mean, self.std, values, old_values, next_values, actions, rewards, dones)

        self.actor_optimizer.zero_grad()
        self.critic_optimizer.zero_grad()

        loss.backward()

        self.actor_optimizer.step() 
        self.critic_optimizer.step() 

    # Update the model
    def update_ppo(self):        
        batch_size  = int(len(self.memory) / self.minibatch)
        dataloader  = DataLoader(self.memory, batch_size, shuffle = False)

        # Optimize policy for K epochs:
        for _ in range(self.PPO_epochs):       
            for states, actions, rewards, dones, next_states in dataloader:
                self.training_ppo(states.float().to(device), actions.float().to(device), rewards.float().to(device), dones.float().to(device), next_states.float().to(device))

        # Clear the memory
        self.memory.clear_memory()

        # Copy new weights into old policy:
        self.actor_old.load_state_dict(self.actor.state_dict())
        self.critic_old.load_state_dict(self.critic.state_dict())

    def save_weights(self):
        torch.save({
            'model_state_dict': self.actor.state_dict(),
            'optimizer_state_dict': self.actor_optimizer.state_dict()
            }, '/test/My Drive/Bipedal4/actor.tar')
        
        torch.save({
            'model_state_dict': self.critic.state_dict(),
            'optimizer_state_dict': self.critic_optimizer.state_dict()
            }, '/test/My Drive/Bipedal4/critic.tar')
        
    def load_weights(self):
        actor_checkpoint = torch.load('/test/My Drive/Bipedal4/actor.tar')
        self.actor.load_state_dict(actor_checkpoint['model_state_dict'])
        self.actor_optimizer.load_state_dict(actor_checkpoint['optimizer_state_dict'])

        critic_checkpoint = torch.load('/test/My Drive/Bipedal4/critic.tar')
        self.critic.load_state_dict(critic_checkpoint['model_state_dict'])
        self.critic_optimizer.load_state_dict(critic_checkpoint['optimizer_state_dict'])

class Runner():
    def __init__(self, env, agent, render, training_mode, n_update):
        self.env = env
        self.agent = agent
        self.render = render
        self.training_mode = training_mode

        self.n_update = n_update
        self.t_updates = 0

    def run_episode(self):
        ############################################
        state = self.env.reset()    
        done = False
        total_reward = 0
        eps_time = 0
        ############################################
        for _ in range(10000): 
            action = self.agent.act(state)   
            next_state, reward, done, _ =  self.env.step(action)

            eps_time += 1 
            self.t_updates += 1
            total_reward += reward
            
            if self.training_mode: 
                self.agent.save_eps(state.tolist(), action, reward, float(done), next_state.tolist()) 
                
            state = next_state
                    
            if self.render:
                self.env.render()     
            
            if self.training_mode and self.n_update is not None and self.t_updates == self.n_update:
                self.agent.update_ppo()
                self.t_updates = 0
            
            if done: 
                break                
        
        if self.training_mode and self.n_update is None:
            self.agent.update_ppo()
                    
        return total_reward, eps_time

def main():
    ############## Hyperparameters ##############
    load_weights        = False # If you want to load the agent, set this to True
    save_weights        = False # If you want to save the agent, set this to True
    training_mode       = True # If you want to train the agent, set this to True. But set this otherwise if you only want to test it
    reward_threshold    = 495 # Set threshold for reward. The learning will stop if reward has pass threshold. Set none to sei this off
    using_google_drive  = False

    render              = False # If you want to display the image, set this to True. Turn this off if you run this in Google Collab
    n_update            = 1024 # How many episode before you update the Policy. Recommended set to 128 for Discrete
    n_plot_batch        = 100000000 # How many episode you want to plot the result
    n_episode           = 100000 # How many episode you want to run
    n_saved             = 10 # How many episode to run before saving the weights

    policy_kl_range     = 0.03 # Recommended set to 0.0008 for Discrete
    policy_params       = 5 # Recommended set to 20 for Discrete
    value_clip          = 1.0 # How many value will be clipped. Recommended set to the highest or lowest possible reward
    entropy_coef        = 0.0 # How much randomness of action you will get
    vf_loss_coef        = 1.0 # Just set to 1
    minibatch           = 32 # How many batch per update. size of batch = n_update / minibatch. Recommended set to 4 for Discrete
    PPO_epochs          = 10 # How many epoch per update. Recommended set to 10 for Discrete
    
    gamma               = 0.99 # Just set to 0.99
    lam                 = 0.95 # Just set to 0.95
    learning_rate       = 3e-4 # Just set to 0.95
    ############################################# 
    writer              = SummaryWriter()

    env_name            = 'BipedalWalker-v3' # Set the env you want
    env                 = gym.make(env_name)

    state_dim           = env.observation_space.shape[0]
    action_dim          = env.action_space.shape[0]

    agent               = Agent(state_dim, action_dim, training_mode, policy_kl_range, policy_params, value_clip, entropy_coef, vf_loss_coef,
                            minibatch, PPO_epochs, gamma, lam, learning_rate) 

    runner              = Runner(env, agent, render, training_mode, n_update)
    #############################################     
    if using_google_drive:
        from google.colab import drive
        drive.mount('/test')

    if load_weights:
        agent.load_weights()
        print('Weight Loaded')

    print('Run the training!!')
    start = time.time()

    try:
        for i_episode in range(1, n_episode + 1):
            total_reward, eps_time = runner.run_episode()

            print('Episode: {} \t t_reward: {} \t time: {} \t '.format(i_episode, total_reward, eps_time))
            writer.add_scalar('rewards', total_reward, i_episode)

            if save_weights:
                if i_episode % n_saved == 0:
                    agent.save_weights() 
                    print('weights saved')

    except KeyboardInterrupt:        
        print('\nTraining has been Shutdown \n')

    finally:
        finish = time.time()
        timedelta = finish - start
        print('Timelength: {}'.format(str( datetime.timedelta(seconds = timedelta) )))    

if __name__ == '__main__':
    main()