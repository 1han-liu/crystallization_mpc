from dataclasses import dataclass

@dataclass
class ModelParams:
    # Physical/kinetic parameters
    k0: float = 1.0e3          # pre-exponential factor (adjust!)
    EA: float = 3.0e4          # activation energy [J/mol] (adjust!)
    R: float  = 8.314          # gas constant
    
    gamma: float = 0.5         # lumped geometry factor rho*S/m_sol (adjust!)
    # Solubility curve coefficients: c_sat(T) = a0 + a1*T + a2*T^2
    a0: float = 30.0
    a1: float = -0.1
    a2: float = 0.0

@dataclass
class ControlParams:
    dt: float = 15.0           # sampling time [s]
    sigma_set: float = 0.05    # target supersaturation
    u_min: float = -0.1        # min cooling rate [degC/s]
    u_max: float = 0.0         # max cooling rate (0 = no heating here)
    T_min: float = 5.0         # bounds for reactor temperature [°C]
    T_max: float = 60.0
    w_rate: float = 0.1        # weight for rate smoothness in cost
    u_prev_init: float = -0.01 # initial previous rate

@dataclass
class EKFParams:
    # Process/measurement noise covariances
    q_c: float = 1e-5          # process noise variance for c
    r_c: float = 1e-3          # measurement noise variance for c
    c_init: float = 30.0       # initial concentration estimate
    P_init: float = 1.0        # initial covariance

# Optional sinks/sources toggles
class IOFlags:
    use_influx = False
    use_rabbit = False

