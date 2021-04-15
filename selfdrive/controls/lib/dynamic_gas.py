import numpy as np
from common.numpy_fast import clip, interp

# dp
DP_OFF = 0
DP_ECO = 1
DP_NORMAL = 2
DP_SPORT = 3

class DynamicGas:
  def __init__(self, CP):
    self.CP = CP
    self.candidate = self.CP.carFingerprint
    self.lead_data = {'v_rel': None, 'a_lead': None, 'x_lead': None, 'status': False}
    self.blinker_status = False
    self.dp_profile = DP_OFF
    self.set_profile()

  def update(self, CS, sm):
    v_ego = CS.vEgo
    self.handle_passable(CS, sm)

    current_dp_profile = sm['dragonConf'].dpAccelProfile
    if self.dp_profile != current_dp_profile:
      self.dp_profile = current_dp_profile
      self.set_profile()

    if self.dp_profile == DP_OFF:
      return float(interp(v_ego, self.CP.gasMaxBP, self.CP.gasMaxV))

    gas = interp(v_ego, self.gasMaxBP, self.gasMaxV)
    if self.lead_data['status']:  # if lead
      x = [0.0, 0.24588812499999999, 0.432818589, 0.593044697, 0.730381365, 1.050833588, 1.3965, 1.714627481]  # relative velocity mod
      y = [0.9901, 0.905, 0.8045, 0.625, 0.431, 0.2083, .0667, 0]
      gas_mod = -(gas * interp(self.lead_data['v_rel'], x, y))

      x = [0.44704, 1.1176, 1.34112]  # lead accel mod
      y = [1.0, 0.75, 0.625]  # maximum we can reduce gas_mod is 40 percent (never increases mod)
      gas_mod *= interp(self.lead_data['a_lead'], x, y)

      # as lead gets further from car, lessen gas mod/reduction
      x = [(i+v_ego) for i in self.x_lead_mod_x ]
      gas_mod *= interp(self.lead_data['x_lead'],x , self.x_lead_mod_y)
      gas = gas + gas_mod

    if (self.blinker_status and self.lead_data['v_rel'] >= 0 ):
      x = [8.9408, 22.352, 31.2928]  # 20, 50, 70 mph
      y = [1.0, 1.115, 1.225]
      gas *= interp(v_ego, x, y)
      
    return float(clip(gas, 0.0, 1.0))

  def set_profile(self):
    self.x_lead_mod_y = [1.0, 0.75, 0.5, 0.25, 0.0] # as lead gets further from car, lessen gas mod/reduction
    x = [0.0, 1.4082, 2.80311, 4.22661, 5.38271, 6.16561, 7.24781, 8.28308, 10.24465, 12.96402, 15.42303, 18.11903, 20.11703, 24.46614, 29.05805, 32.71015, 35.76326, 40]
    if self.dp_profile == DP_ECO:
      #km/h[0,   5,    10,   15,   19,   22,   25,   29,   36,   43,   54,   64,   72,   87,   104,  117,  128   144]
      y = [0.38, 0.40, 0.38, 0.33, 0.33, 0.32, 0.31, 0.30, 0.27, 0.25, 0.24, 0.21, 0.20, 0.20, 0.19, 0.19, 0.17, 0.15]
      self.x_lead_mod_x = [8.1, 12.15, 25.24, 35 , 50 ]
    elif self.dp_profile == DP_SPORT:
      #km/h[0,   5,    10,    15,   19,  22,   25,     29,      36,      43,   54,    64,    72,    87,    104,   117,  128  144]
      y = [0.65, 0.67, 0.63, 0.50, 0.53, 0.53, 0.5229, 0.51784, 0.50765, 0.48, 0.496, 0.509, 0.525, 0.538, 0.45, 0.421, 0.42,0.35]
      self.x_lead_mod_x = [4.1, 6.15, 8.24, 10 , 15 ]
    else:
      #km/h[0,   5,    10,   15,   19,  22,   25,   29,   36,   43,   54,   64,   72,   87,   104,  117,  128   144]
      y = [0.45, 0.42, 0.38, 0.33, 0.3, 0.32, 0.31, 0.30, 0.30, 0.28, 0.24, 0.21, 0.20, 0.20, 0.19, 0.19, 0.17, 0.15]
      self.x_lead_mod_x = [7.1, 10.15, 12.24, 15 , 20 ]

    y = [interp(i, [0.2, (0.2 + 0.45) / 2, 0.45], [1.075 * i, i * 1.05, i]) for i in y]
    self.gasMaxBP, self.gasMaxV = x, y

  def handle_passable(self, CS, sm):
    self.blinker_status = CS.leftBlinker or CS.rightBlinker
    lead_one = sm['radarState'].leadOne
    self.lead_data['v_rel'] = lead_one.vRel
    self.lead_data['a_lead'] = lead_one.aLeadK
    self.lead_data['x_lead'] = lead_one.dRel
    self.lead_data['status'] = sm['plan'].hasLead  # this fixes radarstate always reporting a lead, thanks to arne
