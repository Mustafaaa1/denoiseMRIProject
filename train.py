from tqdm import tqdm
from torch import nn, optim
from torch.utils.data import DataLoader, random_split
from yaml import compose
from utils import pre_data, post_processing, patientDataset, init_weights
from model.unet_model import UNet
from model.unet_MultiDecoder import UNet_MultiDecoders
from model.attention_unet import Atten_Unet
from pathlib import Path
import logging
import torchvision
import wandb
import argparse
import torch

dir_checkpoint = Path('../checkpoints/')

def train_net(dataset, net, device, b, epochs: int=5, batch_size: int=2, learning_rate: float = 1e-5, 
                val_percent: float=0.1, save_checkpoint: bool=True):
    
    # split into training and validation set
    n_val = int(len(dataset) * val_percent)
    n_train = len(dataset) - n_val
    train_set, val_set = random_split(dataset, [n_train, n_val], generator=torch.Generator().manual_seed(0))

    loader_args = dict(batch_size=batch_size, num_workers=4, pin_memory=True)
    train_loader = DataLoader(train_set, shuffle=True, **loader_args)
    val_loader = DataLoader(val_set, shuffle=False, drop_last=True, **loader_args)

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
    
    criterion = nn.MSELoss()
    global_step = 0
    wandb.watch(models=net, criterion=criterion, log="all", log_freq=10)

    post_process= post_processing()

    for epoch in range(1, epochs+1):
        net.train()

        with tqdm(total=n_train, desc=f'Epoch {epoch}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                images = batch
                assert images.shape[1] == net.n_channels, \
                    f'Network has been defined with {net.n_channels} input channels, ' \
                    f'but loaded images have {images.shape[1]} channels. Please check that ' \
                    'the images are loaded correctly.'

                images = images.to(device=device, dtype=torch.float32)

                M, _, _, _, _ = net(images)
                loss =  criterion(M, images) + 1e-6

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                if math.isnan(loss.item()):
                    print("Error: Loss is NaN")

                pbar.update(images.shape[0])
                global_step += 1
                experiment.log({
                    'train loss': loss.item(),
                    'step': global_step,
                    'epoch': epoch
                })
                
            
            with torch.no_grad():
                val_loss, params, M, img = post_process.evaluate(val_loader, net, device)
            scheduler.step(val_loss)
                        
            logging.info('Validation Loss: {}'.format(val_loss))
            experiment.log({'learning rate': optimizer.param_groups[0]['lr'],
                            'validation Loss': val_loss,
                            'Max M': M.cpu().max(),
                            'Min M': M.cpu().min(),
                            'max Image': img.cpu().max(),
                            'min Image': img.cpu().min(),
                            'd1': wandb.Image(params['d1'][0].cpu()),
                            'd2': wandb.Image(params['d2'][0].cpu()),
                            'sigma_g': wandb.Image(params['sigma_g'][0].cpu()),
                            'M': wandb.Image(M.cpu()),
                            'image': wandb.Image(img.cpu()),
                            'epoch': epoch,
                            })
        
        # save the model for the current epoch
        if save_checkpoint:
            Path(dir_checkpoint).mkdir(parents=True, exist_ok=True)
            torch.save(net.state_dict(), str(dir_checkpoint / 'checkpoint_epoch{}.pth'.format(epoch)))
            logging.info(f'Checkpoint {epoch} saved!')
    
    experiment.finish()
    
    
def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images')
    parser.add_argument('--epochs', '-e', metavar='E', type=int, default=30, help='Number of epochs')
    parser.add_argument('--batch-size', '-b', dest='batch_size', metavar='B', type=int, default=1, help='Batch size')
    parser.add_argument('--learning-rate', '-l', metavar='LR', type=float, default=8e-2,
                        help='Learning rate', dest='lr')
    parser.add_argument('--load', '-f', type=str, default=False, help='Load model from a .pth file')
    parser.add_argument('--validation', '-v', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')
    parser.add_argument('--bilinear', action='store_true', default=False, help='Use bilinear upsampling')
    parser.add_argument('--patientData', '-dir', type=str, default='/m2_data/Mustafa_SHARE/save_npy', help='Enther the directory saving the patient data')
    parser.add_argument('--diffusion-direction', '-d', type=str, default='M', help='Enter the diffusion direction: M, I, P or S', 
                        dest='dir')

    return parser.parse_args()

if __name__ == '__main__':
    args = get_args()

    data_dir = args.patientData
    patientData = patientDataset(data_dir, transform=False)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    logging.info(f'Using device {device}')

    b = torch.linspace(0, 2000, steps=21, device=device)
    b = b[1:]
    
    #use the multi-decoders unet
    #net = UNet_MultiDecoders(n_channels=20, b=b, rice=True, bilinear=args.bilinear, attention=False)
    #n_mess = "Unet-MultiDecoders"

    # use standard u-net
    #net = UNet(n_channels=20, b=b, rice=True, bilinear=args.bilinear)
    #n_mess = "Standard Unet"
        
    n_mess = "atten_unet"
    net = Atten_Unet(n_channels=20, b=b, rice=True)

    logging.info(f'Network:\n'
                 f'\t{n_mess}\n'
                 f'\t{net.n_channels} input channels\n'
                 f'\t{"Bilinear" if net.bilinear else "Transposed conv"} upscaling')

    if args.load:
        logging.info(f'Model loaded from {args.load}')
        net.load_state_dict(torch.load(args.load, map_location=device))

    net.to(device=device)
    net.apply(init_weights)
    try:
        train_net(dataset=patientData,
                  net=net,
                  device=device,
                  b = b,
                  epochs=args.epochs,
                  batch_size=args.batch_size,
                  learning_rate=args.lr,
                  val_percent=args.val / 100,
                  )
    except KeyboardInterrupt:
        torch.save(net.state_dict(), 'INTERRUPTED.pth')
        logging.info('Saved interrupt')
        raise
