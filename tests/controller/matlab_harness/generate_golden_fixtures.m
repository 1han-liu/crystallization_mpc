function generate_golden_fixtures(output_file)
%GENERATE_GOLDEN_FIXTURES Produce finite, deterministic R2021a Controller oracles.
% Never executes controller_for_gui.m and never connects to OPC UA.

if nargin < 1
    harness_dir = fileparts(mfilename('fullpath'));
    output_file = fullfile(harness_dir, '..', 'fixtures', 'matlab_r2021a_golden.mat');
end

reference_root = '/home/laniakea/Desktop/iiot-enabled-sensorized-control-platform-for-seeded-crystallization/MPCrystal_original';
source_root = fullfile(reference_root, 'source_codes');
assert(exist(source_root, 'dir') == 7, 'Frozen MATLAB reference worktree not found.');
addpath(genpath(source_root));

run(fullfile(source_root, 'parameters.m'));
run(fullfile(source_root, 'parameters_G.m'));

metadata = struct();
metadata.schema_version = 1;
metadata.baseline_commit = 'ce885a13e0a3e95ac0509e06eebf9d7cd1d418b0';
metadata.matlab_release = version('-release');
metadata.matlab_version = version;
metadata.generated_utc = char(datetime('now', 'TimeZone', 'UTC', 'Format', 'yyyy-MM-dd''T''HH:mm:ssXXX'));
metadata.fixture_name = 'matlab_r2021a_golden';

inputs = struct();
inputs.T = 306.15;
inputs.T_j = 304.15;
inputs.c = 0.31;
inputs.dt = dt;
inputs.x = [306.15; -0.012; 0.31; -2e-6];
inputs.dT_dt_set = -0.025;
inputs.target_sigma = sigma_set;
inputs.target_G = G_set;
inputs.size_list = [180; 220; 250; 300] * 1e-6;

algebra = struct();
algebra.c_sat = calc_c_sat(inputs.T);
algebra.c_meta = calc_c_meta(inputs.T);
algebra.dc_sat_dT = calc_dc_sat_dT(inputs.T);
algebra.relative_sigma = calc_relative_sigma(inputs.c, inputs.T);
[algebra.sigma, algebra.sigma_c_sat] = calc_sigma(inputs.c, inputs.T);
algebra.G_control = calc_G(params, inputs.c, inputs.T, false);
algebra.G_process = calc_G(params, inputs.c, inputs.T, true);
algebra.volume = calc_volume(inputs.size_list);
algebra.surface_area = calc_surface_area(inputs.size_list);
algebra.size_roundtrip = calc_size_from_volume(algebra.volume);
algebra.Q = calc_Q(params, dt);
algebra.R = calc_R(params);
[algebra.C, algebra.Du] = measurement_matrices(params, inputs.x);
algebra.measurement = measurement_function(params, inputs.x);
[algebra.A, algebra.Bu] = state_transition_matrices(params, inputs.x, dt);
[algebra.dx_dt, algebra.ode_A, algebra.ode_Bu] = state_transition_ODE(params, inputs.x, dt);
algebra.x_next = state_transition_function(params, inputs.x, dt);
algebra.T_next = state_transition_function_T(params, inputs.T, inputs.T_j, dt);
algebra.dT_dt_model = calc_dT_dt_model(params, inputs.x, inputs.T_j);
algebra.T_j_calc = calc_T_j(params, inputs.x, inputs.dT_dt_set);
algebra.T_zero = calc_T_with_zero_dT_dt(params, inputs.T_j);
algebra.e_dT_dt = calc_e_dT_dt(params, inputs.x, inputs.T_j, inputs.dT_dt_set);
algebra.e_sigma = calc_e_target(params, 'sigma', sigma_set, inputs.c, inputs.T);
algebra.e_G = calc_e_target(params, 'G', G_set, inputs.c, inputs.T);
algebra.objective_sigma = objective_function(params, inputs.x, 'sigma', inputs.dT_dt_set, sigma_set, dt);
algebra.objective_G = objective_function(params, inputs.x, 'G', inputs.dT_dt_set, G_set, dt);
algebra.lag_perc = calc_t_lag_perc(t_lag_perc, t_lag_threshold_perc_sigma, sigma_set, abs(algebra.e_sigma));
algebra.lag_perc2 = calc_t_lag_perc2(0.2, 0.9, 2.0);
algebra.dT_j_dt_low = calc_dT_j_dt(260, dT_j_dt_max, T_j_min, T_j_max);
algebra.dT_j_dt_middle = calc_dT_j_dt(300, dT_j_dt_max, T_j_min, T_j_max);
algebra.dT_j_dt_high = calc_dT_j_dt(400, dT_j_dt_max, T_j_min, T_j_max);
algebra.updated_T_j = update_T_j(300, 310, dT_j_dt_max, dt, dT_j, dt_update, T_j_min, T_j_max);
[algebra.mass_balance_c, algebra.mass_balance_sizes] = calc_next_crystallization_mass_balance( ...
    params, m_solvent * inputs.c, m_solvent, inputs.size_list, inputs.T, dt);

mode_target = struct([]);
modes = {'MPC', 'PI'};
targets = {'sigma', 'G'};
row = 0;
for mode_index = 1:numel(modes)
    for target_index = 1:numel(targets)
        row = row + 1;
        mode_value = modes{mode_index};
        target_value = targets{target_index};
        if strcmp(target_value, 'sigma')
            target_set_value = sigma_set;
            lower = dT_dt_min_sigma;
            upper = dT_dt_max_sigma;
            params.K_P_T = params.K_P_T_sigma;
            params.K_I_T = params.K_I_T_sigma;
            non_increasing = false;
        else
            target_set_value = G_set;
            lower = dT_dt_min_G;
            upper = dT_dt_max_G;
            params.K_P_T = params.K_P_T_G;
            params.K_I_T = params.K_I_T_G;
            non_increasing = true;
        end
        [dT_set, int_target] = calc_dT_dt_set( ...
            params, inputs.x, mode_value, target_value, target_set_value, lower, upper, dt, 0.25);
        [T_j_set_value, int_dT, int_T] = calc_T_j_set( ...
            params, inputs.x, dT_set, -0.1, 0.2, inputs.T_j, T_j_min, T_j_max, dt, non_increasing);
        mode_target(row).mode = mode_value; %#ok<AGROW>
        mode_target(row).target = target_value;
        mode_target(row).dT_dt_set = dT_set;
        mode_target(row).int_e_target_dt = int_target;
        mode_target(row).T_j_set = T_j_set_value;
        mode_target(row).int_e_dT_dt_dt = int_dT;
        mode_target(row).int_e_T_dt = int_T;
    end
end

replay = struct([]);
replay_T = [306.15, 306.10, 306.04, 305.99, 305.93, 305.88, 305.82, 305.77];
replay_T_j = [305.15, 305.05, 304.95, 304.85, 304.75, 304.65, 304.55, 304.45];
replay_c = [0.3100, 0.3098, 0.3096, 0.3093, 0.3090, 0.3087, 0.3084, 0.3081];
row = 0;
for mode_index = 1:numel(modes)
    for target_index = 1:numel(targets)
        row = row + 1;
        mode_value = modes{mode_index};
        target_value_name = targets{target_index};
        params_case = params;
        if strcmp(target_value_name, 'sigma')
            target_set_value = sigma_set;
            lower = dT_dt_min_sigma;
            upper = dT_dt_max_sigma;
            params_case.K_P_T = params_case.K_P_T_sigma;
            params_case.K_I_T = params_case.K_I_T_sigma;
            lag_threshold = t_lag_threshold_perc_sigma;
            non_increasing = false;
        else
            target_set_value = G_set;
            lower = dT_dt_min_G;
            upper = dT_dt_max_G;
            params_case.K_P_T = params_case.K_P_T_G;
            params_case.K_I_T = params_case.K_I_T_G;
            lag_threshold = t_lag_threshold_perc_G;
            non_increasing = true;
        end
        filter_case = construct_EKF([replay_T(1); 0; replay_c(1); 0], params_case, dt);
        int_target = 0;
        int_dT = 0;
        int_T = 0;
        case_states = zeros(4, numel(replay_T));
        case_sigma = zeros(1, numel(replay_T));
        case_G = zeros(1, numel(replay_T));
        case_dT_set = zeros(1, numel(replay_T));
        case_T_j_set = zeros(1, numel(replay_T));
        case_objective = zeros(1, numel(replay_T));
        for index = 1:numel(replay_T)
            dT_measure = calc_dT_dt(replay_T, dt, index);
            dc_measure = calc_dc_dt(replay_c, dt, index);
            x_measure = [replay_T(index); dT_measure; replay_c(index); dc_measure];
            x_case = smooth_EKF(filter_case, params_case, x_measure);
            case_states(:, index) = x_case;
            case_sigma(index) = calc_sigma(x_case(3), x_case(1));
            case_G(index) = calc_G(params_case, x_case(3), x_case(1));
            if strcmp(target_value_name, 'sigma')
                target_value_case = case_sigma(index);
            else
                target_value_case = case_G(index);
            end
            abs_error_case = abs(target_value_case - target_set_value);
            lag_percentage_case = calc_t_lag_perc(t_lag_perc, lag_threshold, target_set_value, abs_error_case);
            x_projected = x_case + [x_case(2); 0; x_case(4); 0] * t_lag * lag_percentage_case;
            [case_dT_set(index), int_target] = calc_dT_dt_set( ...
                params_case, x_projected, mode_value, target_value_name, ...
                target_set_value, lower, upper, dt, int_target);
            [case_T_j_set(index), int_dT, int_T] = calc_T_j_set( ...
                params_case, x_projected, case_dT_set(index), int_dT, int_T, ...
                replay_T_j(index), T_j_min, T_j_max, dt, non_increasing);
            case_objective(index) = objective_function( ...
                params_case, x_case, target_value_name, case_dT_set(index), target_set_value, dt);
        end
        replay(row).mode = mode_value; %#ok<AGROW>
        replay(row).target = target_value_name;
        replay(row).T = replay_T;
        replay(row).T_j = replay_T_j;
        replay(row).c = replay_c;
        replay(row).states = case_states;
        replay(row).sigma = case_sigma;
        replay(row).G = case_G;
        replay(row).dT_dt_set = case_dT_set;
        replay(row).T_j_set = case_T_j_set;
        replay(row).objective = case_objective;
    end
end

simulation_replay = struct();
simulation_replay.T = zeros(1, 8);
simulation_replay.T_j = zeros(1, 8);
simulation_replay.c = zeros(1, 8);
simulation_replay.T_j_set = [315.15, 313, 311, 309, 307, 305, 303, 301];
simulation_replay.seed_added_at_tick = 3;
simulation_sizes = zeros(size(inputs.size_list));
simulation_T = T_init_sigma;
simulation_T_j = T_j_init;
simulation_c = c_init;
for index = 1:8
    if index == 3
        simulation_sizes = inputs.size_list;
    end
    simulation_replay.T(index) = simulation_T;
    simulation_replay.T_j(index) = simulation_T_j;
    simulation_replay.c(index) = simulation_c;
    if index < 8
        simulation_T = state_transition_function_T(params, simulation_T, simulation_T_j, dt);
        simulation_T_j = update_T_j( ...
            simulation_replay.T_j_set(index), simulation_T_j, dT_j_dt_max, dt, ...
            dT_j, dt_update, T_j_min, T_j_max);
        [simulation_c, simulation_sizes] = calc_next_crystallization_mass_balance( ...
            params, m_solvent * simulation_c, m_solvent, simulation_sizes, ...
            simulation_replay.T(index), dt);
    end
end

scenario_matrix = struct([]);
scenario_matrix(1).run_type = 'experiment';
scenario_matrix(1).growth_rate_source = 'live_gsensor';
scenario_matrix(1).oracle = 'replay';
scenario_matrix(2).run_type = 'simulation';
scenario_matrix(2).growth_rate_source = 'simulated';
scenario_matrix(2).oracle = 'simulation_replay+rng_data';
scenario_matrix(3).run_type = 'simulation';
scenario_matrix(3).growth_rate_source = 'live_gsensor';
scenario_matrix(3).oracle = 'replay';
scenario_matrix(4).run_type = 'simulation';
scenario_matrix(4).growth_rate_source = 'presaved_images';
scenario_matrix(4).oracle = 'replay';

ekf = struct();
ekf.measurements = [306.15, 306.08, 306.02, 305.97; 0.31, 0.3098, 0.3094, 0.3090];
filter = construct_EKF([inputs.T; 0; inputs.c; 0], params, dt);
ekf.states = zeros(4, size(ekf.measurements, 2));
ekf.covariances = zeros(4, 4, size(ekf.measurements, 2));
for index = 1:size(ekf.measurements, 2)
    predict(filter);
    ekf.states(:, index) = correct(filter, ekf.measurements(:, index));
    ekf.covariances(:, :, index) = filter.StateCovariance;
end

general_ekf = struct();
general_ekf.values = [1e-8, 1.2e-8, 1.5e-8, 1.4e-8, 1.8e-8];
general_filter = create_EKF_general(dt, q2_G_measure, r_diag_G_measure, [0; 0; 0]);
general_ekf.filtered = zeros(size(general_ekf.values));
for index = 1:numel(general_ekf.values)
    general_ekf.filtered(index) = smooth_EKF_general( ...
        general_ekf.values(1:index), general_filter, dt);
end

adaptation = struct([]);
adaptation_modes = {'E_A', 'k_0', 'n', 'E_A_and_k_0', 'E_A_and_n', 'k_0_and_n', 'all'};
adapt_sigma = linspace(0.025, 0.065, 45);
adapt_T = linspace(303.15, 313.15, 45);
true_n = 1.62;
true_E_A = 132000;
true_k_0 = params.k_0 * 1.15;
adapt_G = true_k_0 .* adapt_sigma .^ true_n .* exp(-true_E_A ./ params.R ./ adapt_T);
adapt_G([2, 5]) = NaN;
adapt_G(8) = -1;
to_adapt = true(size(adapt_G));
for index = 1:numel(adaptation_modes)
    params_mode = params;
    filter_mode = construct_EKF([inputs.T; 0; inputs.c; 0], params_mode, dt);
    [params_mode, ~, num_adapt] = adapt_growth_parameters( ...
        params_mode, adapt_G, adapt_sigma, adapt_T, to_adapt, 40, filter_mode, dt, 30, adaptation_modes{index});
    adaptation(index).mode = adaptation_modes{index}; %#ok<AGROW>
    adaptation(index).num_adapt = num_adapt;
    adaptation(index).E_A = params_mode.E_A;
    adaptation(index).k_0 = params_mode.k_0;
    adaptation(index).n = params_mode.n;
end

rng_data = struct();
rng_data.tick = (1:40)';
rng_data.T_noise = zeros(40, 1);
rng_data.c_noise = zeros(40, 1);
rng_data.G_noise = zeros(40, 4);
for index = 1:40
    rng(index * 456); rng_data.T_noise(index) = 0.01 * randn(1);
    rng(index * 789); rng_data.c_noise(index) = 0.0002 * randn(1);
    rng(index * 1122); rng_data.G_noise(index, 1) = 1e-9 * randn(1);
    rng(index * 3344); rng_data.G_noise(index, 2) = 1e-9 * randn(1);
    rng(index * 5566); rng_data.G_noise(index, 3) = 1e-9 * randn(1);
    rng(index * 7788); rng_data.G_noise(index, 4) = 1e-9 * randn(1);
end

rng(123);
seed_population = struct();
seed_population.sizes = create_size_list(params, m_seed, d_mean_seed, d_std_seed);
seed_population.total_mass = sum(calc_volume(seed_population.sizes) .* params.rho_solute);
seed_population.requested_mass = m_seed;

if exist(output_file, 'file') == 2
    delete(output_file);
end
save(output_file, 'metadata', 'inputs', 'algebra', 'mode_target', 'replay', ...
    'simulation_replay', 'scenario_matrix', 'ekf', 'general_ekf', ...
    'adaptation', 'rng_data', 'seed_population', '-v7');
fprintf('Wrote %s\n', output_file);
end
