function probe_adaptation_partial_state(input_file, output_file)
% Replay the original adaptation snippet, not a rewritten success/failure path.
% The pre-adaptation measurement/control histories are supplied offline.
assert(strcmp(version('-release'),'2021a'));
assert(~isfile(output_file),'Refuse to overwrite evidence');
root = '/home/laniakea/Desktop/iiot-enabled-sensorized-control-platform-for-seeded-crystallization/MPCrystal_original_matlab/source_codes';
addpath(genpath(root));
data = load(input_file);
run(fullfile(root,'parameters.m'));
params = data.params;
EKF = construct_EKF([315.15;0;.32;0],params,dt);
EKF_G_measure = create_EKF_general(dt,q2_G_measure,r_diag_G_measure,[0;0;0]);
G_measure_list = data.raw_growth(1:61);
G_measure_KF_list = zeros(1,61);
for jj=1:61
    G_measure_KF_list(jj) = smooth_EKF_general(G_measure_list(1:jj),EKF_G_measure,dt);
end
sigma_list = data.sigma(1:61); T_list = data.temperature(1:61);
to_adapt_list = true(1,61); num_adapt = 29; adaptive_disp = true;
n_list = repmat(params.n,1,61); k_0_list = repmat(params.k_0,1,61);
E_A_list = repmat(params.E_A,1,61);
G_u_KF_list = data.raw_growth(1:61); G_v_KF_list = G_u_KF_list;
rows = [];
for ii=62:65
    % Values already written before execute_growth_parameters_adaption.
    T_list(ii) = data.temperature(end); sigma_list(ii) = data.sigma(end);
    G_u_KF_list(ii) = data.raw_growth(end); G_v_KF_list(ii) = data.raw_growth(end);
    state_before = EKF_G_measure.State; params_before = params;
    ok=true; identifier=''; later_steps_executed=false;
    try
        execute_growth_parameters_adaption
        record_growth_parameters
        later_steps_executed=true;
    catch ME
        ok=false; identifier=ME.identifier;
    end
    r=struct('tick',ii,'ok',ok,'identifier',identifier,'num_adapt',num_adapt, ...
        'history_length',length(T_list),'growth_history_length',length(G_measure_KF_list), ...
        'parameter_history_length',length(n_list),'later_steps_executed',later_steps_executed, ...
        'params_unchanged',isequaln(params_before,params), ...
        'growth_filter_changed',~isequaln(state_before,EKF_G_measure.State));
    if isempty(rows), rows=r; else, rows(end+1)=r; end %#ok<AGROW>
end
% Disable adaptation and execute the original subsequent recording snippet.
ii=66; record_growth_parameters
gap_values=n_list(62:65);
save(output_file,'rows','gap_values','n_list','-v7');
disp(rows); disp(gap_values);
end
