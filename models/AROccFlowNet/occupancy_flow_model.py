import einops
import torch
import torch.nn as nn
import torch.nn.functional as F
from configs.utils.config import load_config
from datasets.I24Motion.utils.generate_test_data import SampleModelInput
from models.AROccFlowNet.positional_encoding import generate_3d_sinusoidal_embeddings, generate_2d_sin_pos_embedding, generate_1d_sin_pos_embedding
from models.AROccFlowNet.deformable_transformer import DeformableTransformerDecoder, DeformableTransformerDecoderLayer
from models.AROccFlowNet.convnext_encoder import ConvNeXtFeatureExtractor
from models.AROccFlowNet.efficient_motion_predictor import MotionPredictor
from models.AROccFlowNet.unet_decoder import UNetDecoder

def get_valid_ratio(mask):
        _, H, W = mask.shape
        valid_H = torch.sum(~mask[:, :, 0], 1)
        valid_W = torch.sum(~mask[:, 0, :], 1)
        valid_ratio_h = valid_H.float() / H
        valid_ratio_w = valid_W.float() / W
        valid_ratio = torch.stack([valid_ratio_w, valid_ratio_h], -1)
        return valid_ratio

def get_reference_points(spatial_shapes, valid_ratios, device):
    reference_points_list = []
    for lvl, (H_, W_) in enumerate(spatial_shapes): 
        ref_y, ref_x = torch.meshgrid(torch.linspace(0.5, H_ - 0.5, H_, dtype=torch.float32, device=device),
                                      torch.linspace(0.5, W_ - 0.5, W_, dtype=torch.float32, device=device))
        ref_y = ref_y.reshape(-1)[None] / (valid_ratios[:, None, lvl, 1] * H_)
        ref_x = ref_x.reshape(-1)[None] / (valid_ratios[:, None, lvl, 0] * W_)
        ref = torch.stack((ref_x, ref_y), -1)
        reference_points_list.append(ref)
    reference_points = torch.cat(reference_points_list, 1)
    reference_points = reference_points[:, :, None] * valid_ratios[:, None]
    return reference_points

class AROccFlowNet(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.map_height = config.map_height
        self.map_width = config.map_width

        self.num_waypoints = config.num_waypoints
        self.hidden_dim = config.hidden_dim
        convnext_config = config.convnext
        self.multi_scale_feature_map_encoder = ConvNeXtFeatureExtractor(convnext_config)
        
        # self.motion_predictor = MotionPredictor(config.motionpredictor)
        self.convnext_config = config.convnext
        self.embed_dims = self.convnext_config.embed_dims
        self.shallow_decode = self.convnext_config.shallow_decode
        self.decoder_dim = self.embed_dims[-self.shallow_decode-1]
        self.conv_list = nn.ModuleList([
            nn.Conv2d(in_channels=embed_dim, out_channels= self.decoder_dim, kernel_size=3, stride=1, padding=1) 
            for embed_dim in self.embed_dims[:-self.shallow_decode]
        ])
        
        self.nhead = config.nhead
        self.num_layers = config.num_layers

        deformable_transformer_decoder_layer = DeformableTransformerDecoderLayer(
            d_model= self.decoder_dim, d_ffn=self.hidden_dim, dropout=0.1, activation='relu', n_levels=len(self.embed_dims[:-self.shallow_decode]), n_heads=4, n_points=4
        )
        self.deformable_transformer_decoder = DeformableTransformerDecoder(decoder_layer=deformable_transformer_decoder_layer, num_layers=self.num_layers)
        self.projection_list = nn.ModuleList([
            nn.Linear(in_features=self.hidden_dim, out_features=self.hidden_dim) for _ in range(self.num_waypoints)
        ])
        # self.transformer_decoder_list = nn.ModuleList([
        #     nn.TransformerDecoder(
        #         nn.TransformerDecoderLayer(d_model= self.decoder_dim, nhead=self.nhead, batch_first=True, norm_first=True),
        #         num_layers=self.num_layers
        #     ) for _ in range(self.num_waypoints)
        # ])
        
        
        self.unet_decoder = UNetDecoder(config.unet_decoder)
    def forward(self, his_occupancy_map, his_flow_map, his_observed_agent_features, agent_types, his_valid_mask):
        # //TODO: Modity the input to take his_occupancy_map, his_flow_map, his_observed_agent_features, his_valid_mask, agent_types
        device = his_occupancy_map.device
        cur_occupancy_map = his_occupancy_map
        batch_size, height, width, num_his_points, _ = cur_occupancy_map.shape
        cur_flow_map = his_flow_map

        # agent_embeddings = self.motion_predictor.forward(his_observed_agent_features, agent_types, his_valid_mask) # (B, A, D)
        
        multi_scale_features = self.multi_scale_feature_map_encoder.forward(occupancy_map=cur_occupancy_map, flow_map=cur_flow_map)
        
        
        
        feature_flatten_list = []
        mask_flatten = []
        valid_ratios_list = []
        input_spatial_shapes = []
        # Initialize the memory
        
        for conv, feature in zip(self.conv_list, multi_scale_features):
            feature = conv(feature)
            _, _,feature_map_height, feature_map_width = feature.shape
            input_spatial_shapes.append((feature_map_height, feature_map_width))
            feature = einops.rearrange(feature, 'b c h w -> b (h w) c')
            feature_flatten_list.append(feature)
            mask = torch.zeros(batch_size, feature_map_height, feature_map_width, device=device, dtype=torch.bool)
            valid_ratio = get_valid_ratio(mask)
            valid_ratios_list.append(valid_ratio)
            mask = einops.rearrange(mask, 'b h w -> b (h w)')
            mask_flatten.append(mask)

        valid_ratios = torch.stack(valid_ratios_list, 1)
        mask_flatten = torch.cat(mask_flatten, 1)

        # Prepare the query
        query = feature_flatten_list[-1].clone() # (H/16*W/16, D)
        query_height, query_width = input_spatial_shapes[-1]
        # query_pos = generate_2d_sin_pos_embedding(query_height, query_width,  self.decoder_dim, device)
        # query_pos = einops.rearrange(query_pos, 'h w d -> (h w) d')
        # query_pos = einops.repeat(query_pos, 'n d -> b n d', b=batch_size)
        # Prepare the input for the deformable transformer
        input_flatten = torch.cat(feature_flatten_list, dim=1)
        reference_points = get_reference_points([(query_height, query_width)], valid_ratios, device)
        input_level_start_index =[0]

        for i, (h, w) in enumerate(input_spatial_shapes):
            input_level_start_index.append(input_level_start_index[-1] + h * w)

        input_spatial_shapes = torch.tensor(input_spatial_shapes, device=device)
        input_level_start_index = torch.tensor(input_level_start_index, device=device)
        fused_features, _ = self.deformable_transformer_decoder.forward(
                query, reference_points, input_flatten, input_spatial_shapes, 
                input_level_start_index, valid_ratios,  src_padding_mask=mask_flatten
                # , query_pos=query_pos
            ) # b (h w) d
        fused_features = fused_features + query
        fused_features = einops.repeat(fused_features, 'b n d -> b t n d', t=self.num_waypoints)
        # marginal_features_list = []
        # for timestamp in range(self.num_waypoints):
        #     marginal_features = self.transformer_decoder_list[timestamp](fused_features[:, timestamp], agent_embeddings)
        #     marginal_features_list.append(marginal_features)
        # marginal_features = torch.stack(marginal_features_list, dim=1)
        # res_features = fused_features + marginal_features
        res_features = fused_features
        res_features = einops.rearrange(res_features, 'b t (h w) d -> b t d h w', h=query_height, w=query_width)
        occupancy_map, flow_map = self.unet_decoder(res_features, multi_scale_features)
        

        return occupancy_map, flow_map


if __name__ == '__main__':
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    # ============= Load Configuration =============
    config = load_config('configs/model_configs/AROccFlowNetS.py')
    model_config = config.models
    model = AROccFlowNet(model_config.aroccflownet).to(device)
    dataset_config = config.dataset_config
    sample_data_generator = SampleModelInput(dataset_config)
    test_input = sample_data_generator.generate_sample_input(device=device)
    # ============= Test Forward =============
    his_occupancy_map = test_input['cur/his/occupancy_map']
    his_flow_map = test_input['cur/his/flow_map']
    agent_states = test_input['cur/his/agent_states']
    agent_types = test_input['cur/his/agent_types']
    valid_mask = test_input['cur/his/valid_mask']
    model.forward(his_occupancy_map, his_flow_map, agent_states, agent_types, valid_mask)