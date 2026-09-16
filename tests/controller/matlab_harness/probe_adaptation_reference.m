function probe_adaptation_reference(input_file, output_file)
% Call the unchanged frozen function. No GUI, broker, OPC UA or device access.
assert(strcmp(version('-release'), '2021a'), 'R2021a is required');
assert(~isfile(output_file), 'Refuse to overwrite oracle evidence');
root = '/home/laniakea/Desktop/iiot-enabled-sensorized-control-platform-for-seeded-crystallization/MPCrystal_original_matlab/source_codes';
addpath(genpath(root));
data = load(input_file);
modes = {'E_A','k_0','n','E_A_and_k_0','E_A_and_n','k_0_and_n','all'};
cases = {'tick62','gate29','positive','negative_integer','tiny_negative','zero', ...
         'sigma_nan','temperature_zero','temperature_nan','window'};
rows = struct([]);
for ci = 1:numel(cases)
    for mi = 1:numel(modes)
        p = data.params;
        G = data.growth; s = data.sigma; T = data.temperature;
        selected = logical(data.selected); maximum = data.maximum; minimum = data.minimum;
        if ~strcmp(cases{ci}, 'tick62')
            s = linspace(.025,.065,30); T = linspace(303.15,313.15,30);
            G = p.k_0 * 1.15 * s.^1.62 .* exp(-132000 / p.R ./ T);
            selected = true(size(G));
        end
        switch cases{ci}
            case 'gate29'
                selected(end) = false; s(1) = -.01;
            case 'negative_integer'
                p.n = 2; s(1) = -.01;
            case 'tiny_negative'
                s(1) = -1e-16;
            case 'zero'
                s(1) = 0;
            case 'sigma_nan'
                s(1) = NaN;
            case 'temperature_zero'
                T(1) = 0;
            case 'temperature_nan'
                T(1) = NaN;
            case 'window'
                s = [-.1 s]; T = [310 T]; G = [1e-9 G];
                selected = true(size(G)); maximum = 30;
        end
        EKF = construct_EKF([315.15;0;.32;0], p, 5);
        before_state = EKF.State; before_covariance = EKF.StateCovariance;
        old_p = p; count = 29; ok = true; identifier = ''; message = '';
        try
            [p, EKF, count] = adapt_growth_parameters(p,G,s,T,selected,maximum,EKF,5,minimum,modes{mi});
        catch ME
            ok = false; identifier = ME.identifier; message = ME.message;
        end
        r = struct('name',cases{ci},'mode',modes{mi},'ok',ok,'identifier',identifier, ...
            'message',message,'count',count,'E_A',p.E_A,'k_0',p.k_0,'n',p.n, ...
            'params_unchanged',isequaln(old_p,p), ...
            'filter_state_unchanged',isequaln(before_state,EKF.State), ...
            'filter_covariance_unchanged',isequaln(before_covariance,EKF.StateCovariance), ...
            'growth',G,'sigma',s,'temperature',T,'selected',selected, ...
            'maximum',maximum,'minimum',minimum,'initial_params',old_p);
        if isempty(rows)
            rows = r;
        else
            rows(end+1) = r; %#ok<AGROW>
        end
        fprintf('%s %s ok=%d id=%s count=%d\n',r.name,r.mode,r.ok,r.identifier,r.count);
    end
end
metadata = struct('release',version('-release'),'version',version, ...
    'baseline_commit','ce885a13e0a3e95ac0509e06eebf9d7cd1d418b0');
save(output_file,'rows','metadata','-v7');
end
