import math
from tqdm import tqdm
from torch import nn, optim
from torch.utils.data import DataLoader, random_split
from yaml import compose

from model.res_attention_unet import Res_Atten_Unet
from utils import pre_data, post_processing, patientDataset, init_weights
from model.unet_model import UNet
from model.unet_MultiDecoder import UNet_MultiDecoders
from model.attention_unet import Atten_Unet
from model.unet_model import UNet
from pathlib import Path
import logging
import torchvision
import wandb
import argparse
import torch
from IPython import embed
import numpy as np
from pytorch_msssim import MS_SSIM
dir_checkpoint = Path('../checkpoints/')
def check_gradients(model):
    for name, param in model.named_parameters():
        if param.grad is not None:
            if torch.isnan(param.grad).any():
                print(f"NaN detected in gradient of {name}")

def train_net(dataset, net, device, b,  input_sigma: bool, epochs: int=5, batch_size: int=2, learning_rate: float = 1e-5,
    val_percent: float=0.1, save_checkpoint: bool=True, sweeping = False):
    b = b.reshape(1, len(b), 1, 1)
    # split into training and validation set
    n_val = int(len(dataset) * val_percent)
    n_train = len(dataset) - n_val
    train_set, val_set = random_split(dataset, [n_train, n_val], generator=torch.Generator().manual_seed(0))

    loader_args = dict(batch_size=batch_size, num_workers=4, pin_memory=True)
    train_loader = DataLoader(train_set, shuffle=True, **loader_args)
    val_loader = DataLoader(val_set, shuffle=False, drop_last=True, **loader_args)

    if not sweeping:
        experiment = wandb.init(project='UNet-Denoise', resume='allow', anonymous='must')
        experiment.config.update(dict(epochs=epochs, batch_size=batch_size, learning_rate=learning_rate,
                                      val_percent=val_percent, save_checkpoint=save_checkpoint))

    logging.info(f'''Starting training:
        Epochs:          {epochs}
        Batch size:      {batch_size}
        Learning rate:   {learning_rate}
        Training size:   {n_train}
        Validation size: {n_val}
        Checkpoints:     {save_checkpoint}
        Device:          {device.type}
    ''')

    optimizer = optim.Adam(net.parameters(), lr=learning_rate)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=2)
    
    #criterion = nn.MSELoss()
    criterion = MS_SSIM(channel=20,win_size=5)
    criterion2 = nn.MSELoss()
    global_step = 0
    wandb.watch(models=net, criterion=criterion, log="all", log_freq=10)

    post_process= post_processing()

    for epoch in range(1, epochs+1):
        net.train()
        avg_loss = 0
        num_batches = len(train_loader)

        with tqdm(total=n_train, desc=f'Epoch {epoch}/{epochs}', unit='img') as pbar:
                   #(batch,_,_)
            for i, (images,image_b0,sigma,scale_factor) in enumerate(train_loader):

                images = images.to(device=device, dtype=torch.float32,non_blocking=True)
                sigma = sigma.to(device=device, dtype=torch.float32,non_blocking=True)
                image_b0 = image_b0.to(device=device, dtype=torch.float32,non_blocking=True)
                scale_factor = scale_factor.to(device=device, dtype=torch.float32,non_blocking=True)
                b = b.to(device=device, dtype=torch.float32,non_blocking=True)
                if torch.isnan(images).sum() > 0 or torch.max(images) > 1e10:
                    print(
                        f'-Warning: One batch {i} contained {torch.isnan(images).sum().item()} NaN values and {torch.max(images)} as maximum value.\n This batch was skipped.\n')
                    continue

                if 'parallel' in str(type(net)):
                    assert images.shape[1] == net.module.n_channels, \
                        f'Network has been defined with {net.module.n_channels} input channels, ' \
                        f'but loaded images have {images.shape[1]} channels. Please check that ' \
                        'the images are loaded correctly.'
                else:
                    assert images.shape[1] == net.n_channels, \
                        f'Network has been defined with {net.n_channels} input channels, ' \
                        f'but loaded images have {images.shape[1]} channels. Please check that ' \
                        'the images are loaded correctly.'

                M, _, _, _, _ = net(images,b,image_b0, sigma,scale_factor)
                M = M*scale_factor.view(-1,1,1,1)
                images = images*scale_factor.view(-1,1,1,1)
                criterion.data_range = torch.max(images)

                loss_ssim = 1-criterion(M, images)
                loss_mse = criterion2(M,images)
                loss = loss_ssim*loss_mse
                optimizer.zero_grad()
                loss.backward()

                #check_gradients(net)         
                torch.nn.utils.clip_grad_value_(net.parameters(), clip_value=0.5)
                # Clip gradients to a maximum value
                          
                optimizer.step()
                #if math.isnan(loss.item()):
                #    print("Error: Loss is NaN")
                #    continue

                pbar.update(images.shape[0])
                global_step += 1
                if not sweeping:
                    experiment.log({
                    'train loss': loss.item(),
                    'ssim_loss': loss_ssim.item(),
                    'mse_loss': loss_mse.item(),
                    'step': global_step,
                    'epoch': epoch
                })
                else:
                    wandb.log({
                        'train loss': loss.item(),
                        'step': global_step,
                        'epoch': epoch
                    })
                avg_loss += loss.item()
                if epoch == epochs and i == num_batches-1:
                    np.save('../MimagesTest/images_norm.npy',images.cpu().detach().numpy())
                    np.save('../MimagesTest/M_norm.npy',M.cpu().detach().numpy())
                    print('Saved M and images')
                
            
            with torch.no_grad():
                val_loss, params, M, img,sig = post_process.evaluate(val_loader, net, device, b, input_sigma=input_sigma)
            scheduler.step(val_loss)
                        
            logging.info('Validation Loss: {}'.format(val_loss))
            if not sweeping:
                experiment.log({'learning rate': optimizer.param_groups[0]['lr'],
                            'validation Loss': val_loss,
                            'Max M': M.cpu().max(),
                            'Min M': M.cpu().min(),
                            'max Image': img.cpu().max(),
                            'min Image': img.cpu().min(),
                            'd1': wandb.Image(params['d1'][0].cpu()),
                            'd2': wandb.Image(params['d2'][0].cpu()),
                            'sigma_true' if input_sigma else 'predicted_sigma': wandb.Image(sig.cpu()),
                            'M': wandb.Image(M.cpu()),
                            'image': wandb.Image(img.cpu()),
                            'epoch': epoch,
                            'avg_loss':avg_loss/num_batches
                            })
            else:
                wandb.log({'learning rate': optimizer.param_groups[0]['lr'],
                                'validation Loss': val_loss,
                                'Max M': M.cpu().max(),
                                'Min M': M.cpu().min(),
                                'max Image': img.cpu().max(),
                                'min Image': img.cpu().min(),
                                'epoch': epoch,
                                'avg_loss': avg_loss / num_batches
                                })

        # save the model for the current epoch
        if save_checkpoint:
            Path(dir_checkpoint).mkdir(parents=True, exist_ok=True)
            torch.save(net.state_dict(), str(dir_checkpoint / 'checkpoint_epoch{}.pth'.format(epoch)))
            logging.info(f'Checkpoint {epoch} saved!')
    
    if not sweeping: experiment.finish()
    else: wandb.finish()
    
def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images')
    parser.add_argument('--epochs', '-e', metavar='E', type=int, default=30, help='Number of epochs')
    parser.add_argument('--batch-size', '-b', dest='batch_size', metavar='B', type=int, default=12, help='Batch size')
    parser.add_argument('--learning-rate', '-l', metavar='LR', type=float, default=8e-2,
                        help='Learning rate', dest='lr')
    parser.add_argument('--load', '-f', type=str, default=False, help='Load model from a .pth file')
    parser.add_argument('--validation', '-v', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')
    parser.add_argument('--bilinear', action='store_true', default=False, help='Use bilinear upsampling')
    parser.add_argument('--patientData', '-dir', type=str, default='/m2_data/mustafa/patientData/', help='Enther the directory saving the patient data')
    parser.add_argument('--diffusion-direction', '-d', type=str, default='M', help='Enter the diffusion direction: M, I, P or S', 
                        dest='dir')
    parser.add_argument('--parallel_training', '-parallel', action='store_true', help='Use argument for parallel training with multiple GPUs.')
    parser.add_argument('--sweep', '-sweep', action='store_true', help='Use this flag if you want to run hyper parameter tuning')
    parser.add_argument('--custom_patient_list', '-clist', type=str, default=False, help='Input path to txt file with patient names to be used.')
    parser.add_argument('--input_sigma', '-s', action='store_true', help='Use argument if sigma map is used as input.')




    return parser.parse_args()

def sweep(config = None):

    print('Doing Sweep')

    with wandb.init(config=config):
        # If called by wandb.agent, as below,
        # this config will be set by Sweep Controller
        config = wandb.config
        wandb.run.name = str(f'Batch_size {config.batch_size} num_epochs {config.epochs} lr {config.learning_rate:.4f}')
        try:
            train_net(dataset=patientData,
                      net=net,
                      device=device,
                      b = b,
                      epochs=config.epochs,
                      batch_size=config.batch_size,
                      learning_rate=config.learning_rate,
                      val_percent=args.val / 100,
                      sweeping=True
                      )
        except KeyboardInterrupt:
            torch.save(net.state_dict(), 'INTERRUPTED.pth')
            logging.info('Saved interrupt')
            raise

if __name__ == '__main__':
    args = get_args()
    data_dir = args.patientData

    if args.custom_patient_list:
        with open(args.custom_patient_list, 'r') as file:
            # Read the entire file content and split by commas
            content = file.read().strip()  # Remove leading/trailing whitespace (if any)
            patient_list = content.split(',')
        patientData = patientDataset(data_dir,input_sigma=args.input_sigma,  custom_list=patient_list, transform=False)
    else:
        patientData = patientDataset(data_dir,input_sigma=args.input_sigma, transform=False)



    sweep_config = {
        "name": "sweepDenoiseMRI",
        'method': 'grid',
        'metric': {
            'name': 'avg_loss',
            'goal': 'minimize'},
        'parameters': {
            "learning_rate": {"values": [0.8, 0.08, 0.008]},
            "batch_size": {"values": [2, 4, 8]},
            "epochs": {"values": [1, 2]},
        }
    }
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    logging.info(f'Using device {device}')

    b = torch.linspace(0, 2000, steps=21, device=device)
    b = b[1:]

    n_channels = 20

        

    if torch.cuda.device_count() > 1 & args.parallel_training == True:
        print("Using ", torch.cuda.device_count(), " GPUs!\n")
        n_mess = "atten_unet"
        net = Atten_Unet(n_channels=n_channels, input_sigma=args.input_sigma,rice=True)
        # use the multi-decoders unet
        # net = UNet_MultiDecoders(n_channels=20, b=b, rice=True, bilinear=args.bilinear, attention=False)
        # n_mess = "Unet-MultiDecoders"

        # use standard u-net
        #net = UNet(n_channels=20, b=b, rice=True, bilinear=args.bilinear)
        #n_mess = "Standard Unet"
        #net = Res_Atten_Unet(n_channels=20, b=b, rice=True, bilinear=args.bilinear)
        #n_mess = "Residual Attention Unet"

        # dim = 0 [30, xxx] -> [10, ...], [10, ...], [10, ...] on 3 GPUs
        net = nn.parallel.DistributedDataParallel(net)

        logging.info(f'Network:\n'
                     f'\t{n_mess}\n'
                     f'\t{net.module.n_channels} input channels\n'
                     f'\t{"Bilinear" if net.module.bilinear else "Transposed conv"} upscaling')
    else:
        #n_mess = "atten_unet"
        #net = Atten_Unet(n_channels=n_channels,b=b,rice=True)
        # use the multi-decoders unet
        # net = UNet_MultiDecoders(n_channels=20, b=b, rice=True, bilinear=args.bilinear, attention=False)
        # n_mess = "Unet-MultiDecoders"

        # use standard u-net
        n_mess = "atten_unet"
        net = Atten_Unet(n_channels=n_channels,input_sigma=args.input_sigma, rice=True)
        logging.info(f'Network:\n'
                     f'\t{n_mess}\n'
                     f'\t{net.n_channels} input channels\n'
                     f'\t{"Bilinear" if net.bilinear else "Transposed conv"} upscaling')

    if args.load:
        logging.info(f'Model loaded from {args.load}')
        net.load_state_dict(torch.load(args.load, map_location=device))

    net.to(device=device)
    net.apply(init_weights)

    if args.sweep:
        sweep_id = wandb.sweep(sweep_config, project="Sweep DenoiseMRI")
        wandb.agent(sweep_id, function=sweep)
    else:

        try:
            train_net(dataset=patientData,
                      net=net,
                      device=device,
                      b = b,
                      epochs=args.epochs,
                      batch_size=args.batch_size,
                      learning_rate=args.lr,
                      val_percent=args.val / 100,
                      input_sigma=args.input_sigma)
        except KeyboardInterrupt:
            torch.save(net.state_dict(), 'INTERRUPTED.pth')
            logging.info('Saved interrupt')
            raise
