import os
import sys
sys.path.insert(0, os.getcwd())
import pytorch_lightning as pl
from networks.gan import AnimeGeneratorLite, AnimeDiscriminator, UnetGenerator, SpectNormDiscriminator, AnimeGenerator
from networks.pretrainnet import VGGPreTrained, VGGCaffePreTrained
from datamodules.animegands import AnimeGANDataModule
from datamodules.dsfunction import denormalize
from losses.gan_loss import LSGanLoss
from losses.lsfunction import variation_loss, rgb2yuv
import torch
import torch.nn as nn
from scripts.common import run_common, log_images
from scripts.whiteboxgan import infer_fn


class AnimeGAN(pl.LightningModule):
  GeneratorDict = {
      'AnimeGenerator': AnimeGenerator,
      'AnimeGeneratorLite': AnimeGeneratorLite,
      'UnetGenerator': UnetGenerator,
  }
  DiscriminatorDict = {
      'AnimeDiscriminator': AnimeDiscriminator,
      'SpectNormDiscriminator': SpectNormDiscriminator,
  }

  PreTrainedDict = {
      'VGGPreTrained': VGGPreTrained,
      'VGGCaffePreTrained': VGGCaffePreTrained
  }

  def __init__(
      self,
      lr_g: float = 2e-4,
      lr_d: float = 2e-4,
      g_adv_weight: float = 300.,
      d_adv_weight: float = 300.,
      con_weight: float = 1.5,
      sty_weight: float = 2.8,
      color_weight: float = 10.,
      pre_trained_ckpt: str = None,
      generator_name: str = 'AnimeGeneratorLite',
      discriminator_name: str = 'AnimeDiscriminator',
      pretrained_name: str = 'VGGCaffePreTrained',
      **kwargs
  ):
    super().__init__()
    self.save_hyperparameters()

    # networks
    self.generator = self.GeneratorDict[generator_name]()
    self.pre_trained_ckpt = pre_trained_ckpt
    self.discriminator = self.DiscriminatorDict[discriminator_name]()
    self.lsgan_loss = LSGanLoss()
    self.pretrained = self.PreTrainedDict[pretrained_name]()
    self.l1_loss = nn.L1Loss()
    self.huber_loss = nn.SmoothL1Loss()

  def setup(self, stage: str):
    if stage == 'fit':
      if self.pre_trained_ckpt:
        ckpt = torch.load(self.pre_trained_ckpt)
        generatordict = dict(filter(lambda k: 'generator' in k[0], ckpt['state_dict'].items()))
        generatordict = {k.split('.', 1)[1]: v for k, v in generatordict.items()}
        self.generator.load_state_dict(generatordict, True)
        del ckpt
        del generatordict
        print("Success load pretrained generator from", self.pre_trained_ckpt)

    elif stage == 'test':
      pass

  def on_fit_start(self) -> None:
    self.pretrained.setup(self.device)

  def forward(self, im):
    return self.generator(im)

  def gram(self, x):
    b, c, h, w = x.shape
    gram = torch.einsum('bchw,bdhw->bcd', x, x)
    return gram / (c * h * w)

  def style_loss(self, style, fake):
    return self.l1_loss(self.gram(style), self.gram(fake))

  def con_sty_loss(self, real, anime, fake):
    real_feature_map = self.pretrained(real)
    fake_feature_map = self.pretrained(fake)
    anime_feature_map = self.pretrained(anime)

    c_loss = self.l1_loss(real_feature_map, fake_feature_map)
    s_loss = self.style_loss(anime_feature_map, fake_feature_map)

    return c_loss, s_loss

  def color_loss(self, con, fake):
    con = rgb2yuv(denormalize(con))
    fake = rgb2yuv(denormalize(fake))
    return (self.l1_loss(con[..., 0], fake[..., 0]) +
            self.huber_loss(con[..., 1], fake[..., 1]) +
            self.huber_loss(con[..., 2], fake[..., 2]))

  def discriminator_loss(self, real, gray, fake, real_blur):
    real_loss = torch.mean(torch.square(real - 1.0))
    gray_loss = torch.mean(torch.square(gray))
    fake_loss = torch.mean(torch.square(fake))
    real_blur_loss = torch.mean(torch.square(real_blur))
    return 1.2 * real_loss, 1.2 * gray_loss, 1.2 * fake_loss, 0.8 * real_blur_loss

  def generator_loss(self, fake_logit):
    return self.lsgan_loss._g_loss(fake_logit)

  def training_step(self, batch, batch_idx, optimizer_idx):
    input_photo, (input_cartoon, anime_gray_data), anime_smooth_gray_data = batch

    generated = self.generator(input_photo)
    generated_logit = self.discriminator(generated)

    if optimizer_idx == 0:  # train discriminator
      anime_logit = self.discriminator(input_cartoon)
      anime_gray_logit = self.discriminator(anime_gray_data)
      smooth_logit = self.discriminator(anime_smooth_gray_data)
      (d_real_loss, d_gray_loss,
       d_fake_loss, d_real_blur_loss) = self.discriminator_loss(
          anime_logit, anime_gray_logit,
          generated_logit, smooth_logit)

      d_loss_total = (self.hparams.d_adv_weight * (d_real_loss +
                                                   d_fake_loss +
                                                   d_gray_loss +
                                                   d_real_blur_loss))
      self.log_dict({'dis/d_loss': d_loss_total,
                     'dis/d_real_loss': d_real_loss,
                     'dis/d_fake_loss': d_fake_loss,
                     'dis/d_gray_loss': d_gray_loss,
                     'dis/d_real_blur_loss': d_real_blur_loss, })
      return d_loss_total
    elif optimizer_idx == 1:  # train generator
      c_loss, s_loss = self.con_sty_loss(input_photo, anime_gray_data, generated)
      c_loss = self.hparams.con_weight * c_loss
      s_loss = self.hparams.sty_weight * s_loss
      col_loss = self.color_loss(input_photo, generated) * self.hparams.color_weight
      g_loss = (self.hparams.g_adv_weight * self.generator_loss(generated_logit))
      g_loss_total = c_loss + s_loss + col_loss + g_loss
      self.log_dict({'gen/c_loss': c_loss,
                     'gen/s_loss': s_loss,
                     'gen/col_loss': col_loss,
                     'gen/g_loss': g_loss})
      return g_loss_total

  def configure_optimizers(self):
    opt_d = torch.optim.Adam(self.discriminator.parameters(),
                             lr=self.hparams.lr_d, betas=(0.5, 0.999))
    opt_g = torch.optim.Adam(self.generator.parameters(),
                             lr=self.hparams.lr_g, betas=(0.5, 0.999))
    return opt_d, opt_g

  def validation_step(self, batch, batch_idx):
    input_photo = batch
    log_images(self, {'input/real': input_photo,
                      'generate/anime': self.generator(input_photo)})


if __name__ == "__main__":
  run_common(AnimeGAN, AnimeGANDataModule, infer_fn)
