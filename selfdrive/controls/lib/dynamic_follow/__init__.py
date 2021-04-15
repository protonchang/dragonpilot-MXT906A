import math
import numpy as np
from common.realtime import sec_since_boot
from selfdrive.controls.lib.drive_helpers import MPC_COST_LONG
from common.numpy_fast import interp, clip
from selfdrive.config import Conversions as CV
from common.params import Params
from common.dp_time import LAST_MODIFIED_DYNAMIC_FOLLOW
from common.dp_common import get_last_modified, param_get, param_get_if_updated

from selfdrive.controls.lib.dynamic_follow.auto_df import predict
from selfdrive.controls.lib.dynamic_follow.support import LeadData, CarData, dfData, dfProfiles
travis = False

# dp
PROFILE_AUTO = 4
PROFILE_LONG = 3
PROFILE_NORMAL = 2
PROFILE_SHORT = 1
PROFILE_OFF = 0

class DynamicFollow:
  def __init__(self, mpc_id):
    self.mpc_id = mpc_id
    self.df_profiles = dfProfiles()
    self.global_df_mod = 1.
    self.min_TR = 0.9

    # Model variables
    mpc_rate = 1 / 20.
    self.model_scales = {'v_ego': [-0.06112159043550491, 37.96522521972656], 'a_lead': [-3.109330892562866, 3.3612186908721924], 'v_lead': [0.0, 35.27671432495117], 'x_lead': [2.4600000381469727, 141.44000244140625]}
    self.predict_rate = 1 / 4.
    self.skip_every = round(0.25 / mpc_rate)
    self.model_input_len = round(45 / mpc_rate)

    # Dynamic follow variables
    self.default_TR = 1.8
    self.TR = 1.8
    # self.v_lead_retention = 2.0  # keep only last x seconds
    self.v_ego_retention = 2.5
    self.v_rel_retention = 1.5

    self.sng_TR = 1.8  # reacceleration stop and go TR
    self.sng_speed = 18.0 * CV.MPH_TO_MS

    # dp params
    self.last_ts = 0.
    self.modified = None
    self.last_modified = None
    self.last_modified_check = None
    self.dp_dynamic_follow = PROFILE_OFF
    self.dp_dynamic_follow_last_modified = None
    self.dp_dynamic_follow_multiplier_last_modified = None
    self.dp_dynamic_follow_min_tr_last_modified = None
    self.params = Params()

    self._setup_changing_variables()

  def _setup_changing_variables(self):
    self.TR = self.default_TR
    self.model_profile = None

    self.sng = False
    self.car_data = CarData()
    self.lead_data = LeadData()
    self.df_data = dfData()  # dynamic follow data

    self.last_cost = 0.0
    self.last_predict_time = 0.0
    self.auto_df_model_data = []
    self._get_live_params()  # so they're defined just in case

  def update(self, CS, libmpc):
    self._get_live_params()
    self._update_car(CS)
    self._get_profiles()

    if not self.lead_data.status or self.dp_dynamic_follow == PROFILE_OFF:
      self.TR = self.default_TR
    else:
      self._store_df_data()
      self.TR = self._get_TR()

    if not travis:
      self._change_cost(libmpc)

    return self.TR

  def _get_profiles(self):
    """This receives profile change updates from dfManager and runs the auto-df prediction if auto mode"""
    if self.dp_dynamic_follow == PROFILE_AUTO:  # todo: find some way to share prediction between the two mpcs to reduce processing overhead
      self._get_pred()  # sets self.model_profile, all other checks are inside function

  def _norm(self, x, name):
    self.x = x
    return np.interp(x, self.model_scales[name], [0, 1])

  def _change_cost(self, libmpc):
    TRs = [0.9, 1.8, 2.7]
    costs = [1.0, 0.115, 0.05]
    cost = interp(self.TR, TRs, costs)
    if self.last_cost != cost:
      libmpc.change_tr(MPC_COST_LONG.TTC, cost, MPC_COST_LONG.ACCELERATION, MPC_COST_LONG.JERK)
      self.last_cost = cost

  def _store_df_data(self):
    cur_time = sec_since_boot()
    # Store custom relative accel over time
    if self.lead_data.status:
      if self.lead_data.new_lead:
        self.df_data.v_rels = []  # reset when new lead
      else:
        self.df_data.v_rels = self._remove_old_entries(self.df_data.v_rels, cur_time, self.v_rel_retention)
      self.df_data.v_rels.append({'v_ego': self.car_data.v_ego, 'v_lead': self.lead_data.v_lead, 'time': cur_time})

    # Store our velocity for better sng
    self.df_data.v_egos = self._remove_old_entries(self.df_data.v_egos, cur_time, self.v_ego_retention)
    self.df_data.v_egos.append({'v_ego': self.car_data.v_ego, 'time': cur_time})

    # Store data for auto-df model
    self.auto_df_model_data.append([self._norm(self.car_data.v_ego, 'v_ego'),
                                    self._norm(self.lead_data.v_lead, 'v_lead'),
                                    self._norm(self.lead_data.a_lead, 'a_lead'),
                                    self._norm(self.lead_data.x_lead, 'x_lead')])
    while len(self.auto_df_model_data) > self.model_input_len:
      del self.auto_df_model_data[0]

  def _get_pred(self):
    cur_time = sec_since_boot()
    if self.car_data.cruise_enabled and self.lead_data.status:
      if cur_time - self.last_predict_time > self.predict_rate:
        if len(self.auto_df_model_data) == self.model_input_len:
          pred = predict(np.array(self.auto_df_model_data[::self.skip_every], dtype=np.float32).flatten())
          self.last_predict_time = cur_time
          self.model_profile = int(np.argmax(pred))

  def _remove_old_entries(self, lst, cur_time, retention):
    return [sample for sample in lst if cur_time - sample['time'] <= retention]

  def _calculate_relative_accel_new(self):
    #   """
    #   Moving window returning the following: (final relative velocity - initial relative velocity) / dT with a few extra mods
    #   Output properties:
    #     When the lead is starting to decelerate, and our car remains the same speed, the output decreases (and vice versa)
    #     However when our car finally starts to decelerate at the same rate as the lead car, the output will move to near 0
    #       >>> a = [(15 - 18), (14 - 17)]
    #       >>> (a[-1] - a[0]) / 1
    #       > 0.0
    #   """
    min_consider_time = 0.5  # minimum amount of time required to consider calculation
    if len(self.df_data.v_rels) > 0:  # if not empty
      elapsed_time = self.df_data.v_rels[-1]['time'] - self.df_data.v_rels[0]['time']
      if elapsed_time > min_consider_time:
        x = [-2.6822, -1.7882, -0.8941, -0.447, -0.2235, 0.0, 0.2235, 0.447, 0.8941, 1.7882, 2.6822]
        y = [0.3245, 0.277, 0.11075, 0.08106, 0.06325, 0.0, -0.09, -0.09375, -0.125, -0.3, -0.35]

        v_lead_start = self.df_data.v_rels[0]['v_lead']  # setup common variables
        v_ego_start = self.df_data.v_rels[0]['v_ego']
        v_lead_end = self.df_data.v_rels[-1]['v_lead']
        v_ego_end = self.df_data.v_rels[-1]['v_ego']

        v_ego_change = v_ego_end - v_ego_start
        v_lead_change = v_lead_end - v_lead_start

        if v_lead_change - v_ego_change == 0 or v_lead_change + v_ego_change == 0:
          return None

        initial_v_rel = v_lead_start - v_ego_start
        cur_v_rel = v_lead_end - v_ego_end
        delta_v_rel = (cur_v_rel - initial_v_rel) / elapsed_time

        neg_pos = False
        if v_ego_change == 0 or v_lead_change == 0:  # FIXME: this all is a mess, but works. need to simplify
          lead_factor = v_lead_change / (v_lead_change - v_ego_change)

        elif (v_ego_change < 0) != (v_lead_change < 0):  # one is negative and one is positive, or ^ = XOR
          lead_factor = v_lead_change / (v_lead_change - v_ego_change)
          if v_ego_change > 0 > v_lead_change:
            delta_v_rel = -delta_v_rel  # switch when appropriate
          neg_pos = True

        elif v_ego_change * v_lead_change > 0:  # both are negative or both are positive
          lead_factor = v_lead_change / (v_lead_change + v_ego_change)
          if v_ego_change > 0 and v_lead_change > 0:  # both are positive
            if v_ego_change < v_lead_change:
              delta_v_rel = -delta_v_rel  # switch when appropriate
          elif v_ego_change > v_lead_change:  # both are negative and v_ego_change > v_lead_change
            delta_v_rel = -delta_v_rel

        else:
          raise Exception('Uncovered case! Should be impossible to be be here')

        if not neg_pos:  # negative and positive require different mod code to be correct
          rel_vel_mod = (-delta_v_rel * abs(lead_factor)) + (delta_v_rel * (1 - abs(lead_factor)))
        else:
          rel_vel_mod = math.copysign(delta_v_rel, v_lead_change - v_ego_change) * lead_factor

        calc_mod = np.interp(rel_vel_mod, x, y)
        if v_lead_end > v_ego_end and calc_mod >= 0:
          # if we're accelerating quicker than lead but lead is still faster, reduce mod
          # todo: could remove this since we restrict this mod where called
          x = np.array([0, 2, 4, 8]) * CV.MPH_TO_MS
          y = [1.0, -0.25, -0.65, -0.95]
          v_rel_mod = np.interp(v_lead_end - v_ego_end, x, y)
          calc_mod *= v_rel_mod
        return calc_mod
    return None

  def global_profile_mod(self, profile_mod_x, profile_mod_pos, profile_mod_neg, x_vel, y_dist):
    """
    This function modifies the y_dist list used by dynamic follow in accordance with global_df_mod
    It also intelligently adjusts the profile mods at each breakpoint based on the change in TR
    """
    if self.global_df_mod == 1.:
      return profile_mod_pos, profile_mod_neg, y_dist
    global_df_mod = 1 - self.global_df_mod

    # Calculate new TRs
    speeds = [0, self.sng_speed, 18, x_vel[-1]]  # [0, 18 mph, ~40 mph, highest profile mod speed (~78 mph)]
    mods = [0, 0.1, 0.7, 1]  # how much to limit global_df_mod at each speed, 1 is full effect
    y_dist_new = [y - (y * global_df_mod * np.interp(x, speeds, mods)) for x, y in zip(x_vel, y_dist)]

    # Calculate how to change profile mods based on change in TR
    # eg. if df mod is 0.7, then increase positive mod and decrease negative mod
    calc_profile_mods = [(np.interp(mod_x, x_vel, y_dist) - np.interp(mod_x, x_vel, y_dist_new) + 1) for mod_x in profile_mod_x]
    profile_mod_pos = [mod_pos * mod for mod_pos, mod in zip(profile_mod_pos, calc_profile_mods)]
    profile_mod_neg = [mod_neg * ((1 - mod) + 1) for mod_neg, mod in zip(profile_mod_neg, calc_profile_mods)]

    return profile_mod_pos, profile_mod_neg, y_dist_new

  def _get_TR(self):
    x_vel = [0.0, 1.8627, 3.7253, 5.588, 7.4507, 9.3133, 11.5598, 13.645, 22.352, 31.2928, 33.528, 35.7632, 40.2336]  # velocities
    profile_mod_x = [2.2352, 13.4112, 24.5872, 35.7632]  # profile mod speeds, mph: [5., 30., 55., 80.]

    if self.dp_dynamic_follow == PROFILE_AUTO:  # decide which profile to use, model profile will be updated before this
      # df is 0 = traffic, 1 = relaxed, 2 = roadtrip, 3 = auto
      # dp is 0 = off, 1 = short, 2 = normal, 3 = long, 4 = auto
      # if it's model profile, we need to convert it
      if self.model_profile is None:
        # when its none, we use normal instead
        df_profile = PROFILE_NORMAL
      else:
        df_profile = self.model_profile + 1
    else:
      df_profile = self.dp_dynamic_follow

    if df_profile == PROFILE_LONG:
      y_dist = [1.3978, 1.4132, 1.4318, 1.4536, 1.485, 1.5229, 1.5819, 1.6203, 1.7238, 1.8231, 1.8379, 1.8495, 1.8535]  # TRs
      profile_mod_pos = [0.92, 0.7, 0.25, 0.15]
      profile_mod_neg = [1.1, 1.3, 2.0, 2.3]
    elif df_profile == PROFILE_SHORT:  # for in congested traffic
      x_vel = [0.0, 1.892, 3.7432, 5.8632, 8.0727, 10.7301, 14.343, 17.6275, 22.4049, 28.6752, 34.8858, 40.35]
      # y_dist = [1.3781, 1.3791, 1.3802, 1.3825, 1.3984, 1.4249, 1.4194, 1.3162, 1.1916, 1.0145, 0.9855, 0.9562]  # original
      # y_dist = [1.3781, 1.3791, 1.3112, 1.2442, 1.2306, 1.2112, 1.2775, 1.1977, 1.0963, 0.9435, 0.9067, 0.8749]  # avg. 7.3 ft closer from 18 to 90 mph
      y_dist = [1.3781, 1.3791, 1.3457, 1.3134, 1.3145, 1.318, 1.3485, 1.257, 1.144, 0.979, 0.9461, 0.9156]
      profile_mod_pos = [1.05, 1.55, 2.6, 3.75]
      profile_mod_neg = [0.84, .275, 0.1, 0.05]
    elif df_profile == PROFILE_NORMAL:  # default to relaxed/stock
      y_dist = [1.385, 1.394, 1.406, 1.421, 1.444, 1.474, 1.516, 1.534, 1.546, 1.568, 1.579, 1.593, 1.614]
      profile_mod_pos = [1.0] * 4
      profile_mod_neg = [1.0] * 4
    else:
      raise Exception('Unknown profile type: {}'.format(df_profile))

    # Global df mod
    profile_mod_pos, profile_mod_neg, y_dist = self.global_profile_mod(profile_mod_x, profile_mod_pos, profile_mod_neg, x_vel, y_dist)

    # Profile modifications - Designed so that each profile reacts similarly to changing lead dynamics
    profile_mod_pos = interp(self.car_data.v_ego, profile_mod_x, profile_mod_pos)
    profile_mod_neg = interp(self.car_data.v_ego, profile_mod_x, profile_mod_neg)

    if self.car_data.v_ego > self.sng_speed:  # keep sng distance until we're above sng speed again
      self.sng = False

    if (self.car_data.v_ego >= self.sng_speed or self.df_data.v_egos[0]['v_ego'] >= self.car_data.v_ego) and not self.sng:
      # if above 15 mph OR we're decelerating to a stop, keep shorter TR. when we reaccelerate, use sng_TR and slowly decrease
      TR = interp(self.car_data.v_ego, x_vel, y_dist)
    else:  # this allows us to get closer to the lead car when stopping, while being able to have smooth stop and go when reaccelerating
      self.sng = True
      x = [self.sng_speed * 0.7, self.sng_speed]  # decrease TR between 12.6 and 18 mph from 1.8s to defined TR above at 18mph while accelerating
      y = [self.sng_TR, interp(self.sng_speed, x_vel, y_dist)]
      TR = interp(self.car_data.v_ego, x, y)

    TR_mods = []
    # Dynamic follow modifications (the secret sauce)
    x = [-26.8224, -20.0288, -15.6871, -11.1965, -7.8645, -4.9472, -3.0541, -2.2244, -1.5045, -0.7908, -0.3196, 0.0, 0.5588, 1.3682, 1.898, 2.7316, 4.4704]  # relative velocity values
    y = [.76, 0.62323, 0.49488, 0.40656, 0.32227, 0.23914, 0.12269, 0.10483, 0.08074, 0.04886, 0.0072, 0.0, -0.05648, -0.0792, -0.15675, -0.23289, -0.315]  # modification values
    TR_mods.append(interp(self.lead_data.v_lead - self.car_data.v_ego, x, y))

    x = [-4.4795, -2.8122, -1.5727, -1.1129, -0.6611, -0.2692, 0.0, 0.1466, 0.5144, 0.6903, 0.9302]  # lead acceleration values
    y = [0.24, 0.16, 0.092, 0.0515, 0.0305, 0.022, 0.0, -0.0153, -0.042, -0.053, -0.059]  # modification values
    TR_mods.append(interp(self.lead_data.a_lead, x, y))

    rel_accel_mod = self._calculate_relative_accel_new()
    if rel_accel_mod is not None:  # if available
      deadzone = 2 * CV.MPH_TO_MS
      if self.lead_data.v_lead - deadzone > self.car_data.v_ego:
        TR_mods.append(rel_accel_mod)

    x = [self.sng_speed / 5.0, self.sng_speed]  # as we approach 0, apply x% more distance
    y = [1.05, 1.0]
    profile_mod_pos *= interp(self.car_data.v_ego, x, y)  # but only for currently positive mods

    TR_mod = sum([mod * profile_mod_neg if mod < 0 else mod * profile_mod_pos for mod in TR_mods])  # alter TR modification according to profile
    TR += TR_mod

    if self.car_data.left_blinker or self.car_data.right_blinker and df_profile != self.df_profiles.traffic:
      x = [8.9408, 22.352, 31.2928]  # 20, 50, 70 mph
      y = [1.0, .75, .65]
      TR *= interp(self.car_data.v_ego, x, y)  # reduce TR when changing lanes

    return float(clip(TR, self.min_TR, 2.7))

  def update_lead(self, v_lead=None, a_lead=None, x_lead=None, status=False, new_lead=False):
    self.lead_data.v_lead = v_lead
    self.lead_data.a_lead = a_lead
    self.lead_data.x_lead = x_lead

    self.lead_data.status = status
    self.lead_data.new_lead = new_lead

  def _update_car(self, CS):
    self.car_data.v_ego = CS.vEgo
    self.car_data.a_ego = CS.aEgo

    self.car_data.left_blinker = CS.leftBlinker
    self.car_data.right_blinker = CS.rightBlinker
    self.car_data.cruise_enabled = CS.cruiseState.enabled

  def _get_live_params(self):
    self.last_modified_check, self.modified = get_last_modified(LAST_MODIFIED_DYNAMIC_FOLLOW, self.last_modified_check, self.modified)
    if self.last_modified != self.modified:
      self.dp_dynamic_follow, self.dp_dynamic_follow_last_modified = param_get_if_updated("dp_dynamic_follow", "int", self.dp_dynamic_follow, self.dp_dynamic_follow_last_modified)
      self.global_df_mod, self.dp_dynamic_follow_multiplier_last_modified = param_get_if_updated("dp_dynamic_follow_multiplier", "float", self.global_df_mod, self.dp_dynamic_follow_multiplier_last_modified)
      if self.global_df_mod != 1.:
        self.global_df_mod = clip(self.global_df_mod, .85, 1.2)
      self.min_TR, self.dp_dynamic_follow_min_tr_last_modified = param_get_if_updated("dp_dynamic_follow_min_tr", "float", self.min_TR, self.dp_dynamic_follow_min_tr_last_modified)
      if self.min_TR != .9:
        self.min_TR = clip(self.min_TR, .85, 1.6)
      self.last_modified = self.modified