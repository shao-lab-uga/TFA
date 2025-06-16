
import numpy as np


if __name__ == "__main__":
    # config = load_config("configs/model_configs/AROccFlowNetS.py")
    # occupancy_map_config = config.dataset_config.occupancy_flow_map
    test_data = np.load("", allow_pickle=True).item()
    test_data = test_data['cur']
    k = 2
    # validate_flow_wrap_occupancy(test_data, occupancy_map_config, k)
    trajs = test_data['pred/trajectories']
    print(test_data['pred/trajectories'].shape)
    num_agents, num_timesteps, num_features = trajs.shape
    # visualize the trajectory
    import matplotlib.pyplot as plt
    plt.figure()
    for i in range(num_agents):
        print(trajs[i, :, 0])
        print(trajs[i, :, 1])