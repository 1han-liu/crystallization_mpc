import math
from dataclasses import dataclass
from typing import Tuple
import numpy as np

from config import EKFParams, ControlParams, ModelParams
from physics import dc_dt, df_dc

class EKF1C:
    """Extended Kalman Filter for 1D concentration state:
       x = c
       z = c_meas
       dynamics: c_{k+1} = c_k + dt * f(c_k, T_k)
       measurement: z_k = c_k + v
    """
    def __init__(self, ekf_params: EKFParams, ctrl: ControlParams, model: ModelParams):
        self.p = ekf_params
        self.ctrl = ctrl
        self.model = model
        self.c = ekf_params.c_init
        self.P = ekf_params.P_init

    def predict(self, T_c: float):
        dt = self.ctrl.dt
        # Nonlinear prediction
        f = dc_dt(self.c, T_c, self.model)
        c_pred = self.c + dt * f

        # Linearized state transition: F = 1 + dt * df/dc
        F = 1.0 + dt * df_dc(self.c, T_c, self.model)
        Q = self.p.q_c  # process noise variance

        P_pred = F * self.P * F + Q
        self.c, self.P = c_pred, P_pred
        return self.c, self.P

    def update(self, z_c: float):
        # Measurement model h(c) = c, H = 1
        H = 1.0
        R = self.p.r_c

        # Innovation
        y = z_c - self.c
        S = H * self.P * H + R
        K = (self.P * H) / S

        # Correct
        self.c = self.c + K * y
        self.P = (1 - K * H) * self.P
        return self.c, self.P

