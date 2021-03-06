"""
Copyright (C) 2013-2017 Stefan Pfenninger.
Licensed under the Apache 2.0 License (see LICENSE file).

base.py
~~~~~~~

Basic model constraints.

"""

import pyomo.core as po  # pylint: disable=import-error
import numpy as np

from .. import exceptions
from .. import transmission
from .. import utils

def get_constraint_param(model, param_string, y, x, t):
    """
    Function to get values for constraints which can optionally be
    loaded from file (so may have time dependency).

    model = calliope model
    param_string = constraint as string
    y = technology
    x = location
    t = timestep
    """

    if param_string in model.data and y in model._sets['y_def_' + param_string]:
        return getattr(model.m, param_string)[y, x, t]
    else:
        return model.get_option(y + '.constraints.' + param_string, x=x)

def get_cost_param(model, param_string, k, y, x, t):
    """
    Function to get values for constraints which can optionally be
    loaded from file (so may have time dependency).

    model = calliope model
    cost = cost name, e.g. 'om_fuel'
    k = cost type, e.g. 'monetary'
    y = technology
    x = location
    t = timestep
    """
    cost_getter = utils.cost_getter(model.get_option)

    @utils.memoize
    def _cost(cost, y, k, x=None):
        return cost_getter(cost, y, k, x=x)

    if param_string in model.data and y in model._sets['y_def_' + param_string]:
        return getattr(model.m, param_string)[y, x, t, k]
    else: # Search in model.config_model
        return _cost(param_string, y, k, x=x)

def node_resource(model):
    """
    Defines variables:

    * rs: resource <-> storage (+ production, - consumption)
    * r_area: resource collector area
    * rbs: secondary resource -> storage (+ production)

    """
    m = model.m

    # Variables
    m.rs = po.Var(m.y, m.x, m.t, within=po.Reals)
    m.r_area = po.Var(m.y_def_r, m.x, within=po.NonNegativeReals)
    m.rbs = po.Var(m.y_rb, m.x, m.t, within=po.NonNegativeReals)

    # Constraint rules
    def c_rs_rule(m, y, x, t):
        r_scale = model.get_option(y + '.constraints.r_scale', x=x)
        r_eff = get_constraint_param(model, 'r_eff', y, x, t)
        force_r = get_constraint_param(model, 'force_r', y, x, t)
        r_avail = (m.r[y, x, t]
                   * r_scale
                   * m.r_area[y, x]
                   * r_eff)
        if force_r:
            return m.rs[y, x, t] == r_avail
        # TODO reformulate conditionally once Pyomo supports that:
        # had to remove the following formulation because it is not
        # re-evaluated on model re-construction -- we now check for
        # demand/supply tech instead, which means that `r` can only
        # be ALL negative or ALL positive for a given tech!
        # elif po.value(m.r[y, x, t]) > 0:
        elif (y in model.get_group_members('supply') or
              y in model.get_group_members('unmet_demand')):
            return m.rs[y, x, t] <= r_avail
        elif y in model.get_group_members('demand'):
            return m.rs[y, x, t] >= r_avail

    # Constraints
    m.c_rs = po.Constraint(m.y_def_r, m.x, m.t, rule=c_rs_rule)


def node_energy_balance(model):
    """
    Defines variables:

    * s: storage level
    * es_prod: storage -> carrier (+ production)
    * es_con: storage <- carrier (- consumption)
    * export: storage -> export link

    """
    m = model.m
    d = model.data
    time_res = model.data['_time_res'].to_series()

    def get_e_eff_per_distance(model, y, x):
        e_loss = model.get_option(y + '.constraints_per_distance.e_loss', x=x)
        per_distance = model.get_option(y + '.per_distance')
        tech, x2 = y.split(':')
        link = model.config_model.get_key('links.'+ x + ',' + x2,
            default=model.config_model['links'].get(x2 + ',' + x))
        # link = None if no link exists
        if not link:
            return 1.0
        try:
            distance = link.get_key(tech + '.distance')
        except KeyError:
            if e_loss > 0:
                e = exceptions.OptionNotSetError
                raise e('Distance must be defined for link: {} '
                        'and transmission tech: {}, as e_loss per distance '
                        'is defined'.format(x + ',' + x2, tech))
            else:
                return 1.0
        return 1 - (e_loss * (distance / per_distance))

    # Variables
    m.s = po.Var(m.y_pc, m.x, m.t, within=po.NonNegativeReals)
    m.es_prod = po.Var(m.c, m.y, m.x, m.t, within=po.NonNegativeReals)
    m.es_con = po.Var(m.c, m.y, m.x, m.t, within=po.NegativeReals)
    m.export = po.Var(m.y_export, m.x, m.t, within=po.NonNegativeReals)

    # Constraint rules
    def transmission_rule(m, y, x, t):
        y_remote, x_remote = transmission.get_remotes(y, x)
        e_eff = get_constraint_param(model, 'e_eff', y, x, t)
        if y_remote in m.y_trans:
            c = model.get_option(y + '.carrier')
            return (m.es_prod[c, y, x, t]
                    == -1 * m.es_con[c, y_remote, x_remote, t]
                    * e_eff
                    * get_e_eff_per_distance(model, y, x))
        else:
            return po.Constraint.NoConstraint

    def conversion_rule(m, y, x, t):
        c_prod = model.get_option(y + '.carrier')
        c_source = model.get_option(y + '.source_carrier')
        e_eff = get_constraint_param(model, 'e_eff', y, x, t)
        export = m.export[y, x, t] if model.get_option(y + '.export', x=x) else 0
        return (m.es_prod[c_prod, y, x, t] + export
                == -1 * m.es_con[c_source, y, x, t] * e_eff)

    def pc_rule(m, y, x, t):
        e_eff = get_constraint_param(model, 'e_eff', y, x, t)
        # TODO once Pyomo supports it,
        # let this update conditionally on param update!
        if po.value(e_eff) == 0:
            e_prod = 0
        else:
            e_prod = sum(m.es_prod[c, y, x, t] for c in m.c) / e_eff
        e_con = sum(m.es_con[c, y, x, t] for c in m.c) * e_eff

        # If this tech is in the set of techs allowing rb, include it
        if y in m.y_rb:
            rbs = m.rbs[y, x, t]
        else:
            rbs = 0

        # If this tech allows export outside the system, include it
        export = m.export[y, x, t] / e_eff \
            if model.get_option(y + '.export', x=x) else 0

        # A) Case where no storage allowed
        s_cap_max = model.get_option(y + '.constraints.s_cap.max', x=x)
        use_s_time = get_constraint_param(model, 'use_s_time', y, x, t)
        if ( s_cap_max == 0 and
                not use_s_time):
            return m.rs[y, x, t] == e_prod + e_con + export - rbs

        # B) Case where storage is allowed
        else:
            # Ensure that storage-only techs have no rs
            if y in model.get_group_members('storage'):
                rs = 0
            else:
                rs = m.rs[y, x, t]
            m.rs[y, x, t]
            # set up s_minus_one
            # NB: From Pyomo 3.5 to 3.6, order_dict became zero-indexed
            if m.t.order_dict[t] == 0:
                s_minus_one = m.s_init[y, x]
            else:
                s_loss = get_constraint_param(model, 's_loss', y, x, t)
                s_minus_one = (((1 - s_loss)
                                ** time_res.at[model.prev_t(t)])
                               * m.s[y, x, model.prev_t(t)])
            return (m.s[y, x, t] == s_minus_one + rs
                    + rbs - e_prod - e_con - export)

    # Constraints
    m.c_s_balance_transmission = po.Constraint(m.y_trans, m.x, m.t,
                                               rule=transmission_rule)
    m.c_s_balance_conversion = po.Constraint(m.y_conv, m.x, m.t,
                                             rule=conversion_rule)
    m.c_s_balance_pc = po.Constraint(m.y_pc, m.x, m.t, rule=pc_rule)


def node_constraints_build(model):
    """
    Defines variables:

    * s_cap: installed storage capacity
    * r_cap: installed resource <-> storage conversion capacity
    * e_cap: installed storage <-> grid conversion capacity (gross)
    * e_cap_net: installed storage <-> grid conversion capacity (net)
    * rb_cap: installed secondary resource conversion capacity

    """
    m = model.m
    d = model.data

    def get_var_constraint(model_var, y, var, x,
                           _equals=None, _max=None, _min=None,
                           scale=None):

        if not _equals:
            _equals = model.get_option(y + '.constraints.'
                                       + var + '.equals', x=x)
        if not _max:
            _max = model.get_option(y + '.constraints.' + var + '.max', x=x)
        if not _min:
            _min = model.get_option(y + '.constraints.' + var + '.min', x=x)
        if scale:
            _equals = scale * _equals
            _min = scale * _min
            _max = scale * _max
        if _equals:
            if np.isinf(_equals):
                e = exceptions.ModelError
                raise e('Cannot use inf in operational mode, for value of '
                        '{}.{}.equals.{}'.format(y, var, x))
            return model_var == _equals
        elif model.mode == 'operate':
            # Operational mode but 'equals' constraint not set, we use 'max'
            # instead
            # FIXME this should be logged
            if np.isinf(_max):
                return po.Constraint.NoConstraint
            else:
                return model_var == _max
        else:
            if np.isinf(_max):
                _max = None  # to disable upper bound
            if _min == 0 and _max is None:
                return po.Constraint.NoConstraint
            else:
                return (_min, model_var, _max)

    # Variables
    m.s_cap = po.Var(m.y_pc, m.x, within=po.NonNegativeReals)
    m.r_cap = po.Var(m.y_def_r, m.x, within=po.NonNegativeReals)
    m.e_cap = po.Var(m.y, m.x, within=po.NonNegativeReals)
    m.e_cap_net = po.Var(m.y, m.x, within=po.NonNegativeReals)
    m.rb_cap = po.Var(m.y_rb, m.x, within=po.NonNegativeReals)

    # Constraint rules
    def c_s_cap_rule(m, y, x):
        if model.get_option(y + '.constraints.use_s_time', x=x):
            scale = model.get_option(y + '.constraints.e_cap_scale', x=x)
            s_time_max = model.get_option(y + '.constraints.s_time.max', x=x)
            e_cap = model.get_option(y + '.constraints.e_cap.equals', x=x)
            if not e_cap:
                e_cap = model.get_option(y + '.constraints.e_cap.max', x=x)
            e_eff_ref = model.get_eff_ref('e', y)
            s_cap_max = s_time_max * e_cap * scale / e_eff_ref
        else:
            s_cap_max = None

        return get_var_constraint(m.s_cap[y, x], y, 's_cap', x, _max=s_cap_max)

    def c_r_cap_rule(m, y, x):
        if model.get_option(y + '.constraints.r_cap_equals_e_cap', x=x):
            return m.r_cap[y, x] == m.e_cap[y, x]
        else:
            return get_var_constraint(m.r_cap[y, x], y, 'r_cap', x)

    def c_r_area_rule(m, y, x):
        area_per_cap = model.get_option(y + '.constraints.r_area_per_e_cap', x=x)
        if area_per_cap:
            return m.r_area[y, x] == m.e_cap[y, x] * area_per_cap
        else:
            e_cap_max = model.get_option(y + '.constraints.e_cap.max', x=x)
            if e_cap_max == 0:
                # If a technology has no e_cap here, we force r_area to zero,
                # so as not to accrue spurious costs
                return m.r_area[y, x] == 0
            elif model.get_option(y + '.constraints.r_area.max', x=x) is False:
                return m.r_area[y, x] == 1
            else:
                return get_var_constraint(m.r_area[y, x], y, 'r_area', x)

    def c_e_cap_rule(m, y, x):
        # First check whether this tech is allowed at this location
        if not model._locations.at[x, y] == 1:
            return m.e_cap[y, x] == 0
        else:
            e_cap_scale = model.get_option(y + '.constraints.e_cap_scale', x=x)
            return get_var_constraint(m.e_cap[y, x], y, 'e_cap', x,
                                      scale=e_cap_scale)

    def c_e_cap_gross_net_rule(m, y, x):
        c_eff = model.get_option(y + '.constraints.c_eff', x=x)
        return m.e_cap[y, x] * c_eff == m.e_cap_net[y, x]

    def c_rb_cap_rule(m, y, x):
        follow = model.get_option(y + '.constraints.rb_cap_follow', x=x)
        mode = model.get_option(y + '.constraints.rb_cap_follow_mode', x=x)

        # First deal with the special case of ``rb_cap_follow`` being set
        if follow:
            if follow == 'r_cap':
                rb_cap_val = m.r_cap[y, x]
            elif follow == 'e_cap':
                rb_cap_val = m.e_cap[y, x]
            elif follow is not False:
                # Raise an error to make sure follows isn't accidentally set to
                # something invalid
                e = exceptions.ModelError
                raise e('rb_cab_follow set to invalid value at '
                        '({}, {}): {}'.format(y, x, follow))

            if mode == 'max':
                return m.rb_cap[y, x] <= rb_cap_val
            elif mode == 'equals':
                return m.rb_cap[y, x] == rb_cap_val

        else:  # If ``rb_cap_follow`` not set, set up standard constraints
            return get_var_constraint(m.rb_cap[y, x], y, 'rb_cap', x)

    # Constraints
    m.c_s_cap = po.Constraint(m.y_pc, m.x, rule=c_s_cap_rule)
    m.c_r_cap = po.Constraint(m.y_def_r, m.x, rule=c_r_cap_rule)
    m.c_r_area = po.Constraint(m.y_def_r, m.x, rule=c_r_area_rule)
    m.c_e_cap = po.Constraint(m.y, m.x, rule=c_e_cap_rule)
    m.c_e_cap_gross_net = po.Constraint(m.y, m.x, rule=c_e_cap_gross_net_rule)
    m.c_rb_cap = po.Constraint(m.y_rb, m.x, rule=c_rb_cap_rule)


def node_constraints_operational(model):
    m = model.m
    time_res = model.data['_time_res'].to_series()

    # Constraint rules
    def c_rs_max_upper_rule(m, y, x, t):
        return m.rs[y, x, t] <= time_res.at[t] * m.r_cap[y, x]

    def c_rs_max_lower_rule(m, y, x, t):
        return m.rs[y, x, t] >= -1 * time_res.at[t] * m.r_cap[y, x]

    def c_es_prod_max_rule(m, c, y, x, t):
        e_prod = get_constraint_param(model, 'e_prod', y, x, t)
        export = m.export[y, x, t] if model.get_option(y + '.export', x=x) else 0
        if (e_prod is True and
                c == model.get_option(y + '.carrier')):
            return m.es_prod[c, y, x, t] + export <= time_res.at[t] * m.e_cap[y, x]
        else:
            return m.es_prod[c, y, x, t] + export == 0

    def c_es_prod_min_rule(m, c, y, x, t):
        min_use = get_constraint_param(model, 'e_cap_min_use', y, x, t)
        export = m.export[y, x, t] if model.get_option(y + '.export', x=x) else 0
        if (min_use and c == model.get_option(y + '.carrier')):
            return (m.es_prod[c, y, x, t] + export
                    >= time_res.at[t] * m.e_cap[y, x] * min_use)
        else:
            return po.Constraint.NoConstraint

    def c_es_con_max_rule(m, c, y, x, t):
        e_con = get_constraint_param(model, 'e_con', y, x, t)
        if y in m.y_conv:
            return po.Constraint.Skip
        else:
            carrier = '.carrier'
        if (e_con is True and
                c == model.get_option(y + carrier)):
            return m.es_con[c, y, x, t] >= (-1 * time_res.at[t]
                                            * m.e_cap[y, x])
        else:
            return m.es_con[c, y, x, t] == 0

    def c_s_max_rule(m, y, x, t):
        return m.s[y, x, t] <= m.s_cap[y, x]

    def c_rbs_max_rule(m, y, x, t):
        rb_startup = get_constraint_param(model, 'rb_startup_only', y, x, t)
        if (rb_startup and t >= model.data.startup_time_bounds):
            return m.rbs[y, x, t] == 0
        else:
            return m.rbs[y, x, t] <= time_res.at[t] * m.rb_cap[y, x]

    # Constraints
    m.c_rs_max_upper = po.Constraint(m.y_def_r, m.x, m.t,
                                     rule=c_rs_max_upper_rule)
    m.c_rs_max_lower = po.Constraint(m.y_def_r, m.x, m.t,
                                     rule=c_rs_max_lower_rule)
    m.c_es_prod_max = po.Constraint(m.c, m.y, m.x, m.t,
                                    rule=c_es_prod_max_rule)
    m.c_es_prod_min = po.Constraint(m.c, m.y, m.x, m.t,
                                    rule=c_es_prod_min_rule)
    m.c_es_con_max = po.Constraint(m.c, m.y, m.x, m.t,
                                   rule=c_es_con_max_rule)
    m.c_s_max = po.Constraint(m.y_pc, m.x, m.t,
                              rule=c_s_max_rule)
    m.c_rbs_max = po.Constraint(m.y_rb, m.x, m.t,
                                rule=c_rbs_max_rule)


def node_constraints_transmission(model):
    """
    Constrains e_cap symmetrically for transmission nodes.

    """
    m = model.m

    # Constraint rules
    def c_trans_rule(m, y, x):
        y_remote, x_remote = transmission.get_remotes(y, x)
        if y_remote in m.y_trans:
            return m.e_cap[y, x] == m.e_cap[y_remote, x_remote]
        else:
            return po.Constraint.NoConstraint

    # Constraints
    m.c_transmission_capacity = po.Constraint(m.y_trans, m.x,
                                              rule=c_trans_rule)


def node_parasitics(model):
    """
    Additional variables and constraints for plants with internal parasitics.

    Defines variables:

    * ec_prod: storage -> carrier after parasitics (+ production)
    * ec_con: storage <- carrier after parasitics (- consumption)

    """
    m = model.m

    # Variables
    m.ec_prod = po.Var(m.c, m.y_p, m.x, m.t, within=po.NonNegativeReals)
    m.ec_con = po.Var(m.c, m.y_p, m.x, m.t, within=po.NegativeReals)

    # Constraint rules
    def c_ec_prod_rule(m, c, y, x, t):
        return (m.ec_prod[c, y, x, t]
                == m.es_prod[c, y, x, t]
                * model.get_option(y + '.constraints.c_eff', x=x))

    def c_ec_con_rule(m, c, y, x, t):
        if y in m.y_trans or y in m.y_conv:
            # Ensure that transmission and conversion technologies
            # do not double count c_eff
            c_eff = 1.0
        else:
            c_eff = model.get_option(y + '.constraints.c_eff', x=x)
        if c_eff > 0:
            return (m.ec_con[c, y, x, t]
                    == m.es_con[c, y, x, t]
                    / c_eff)
        else:
            return (m.ec_con[c, y, x, t] == 0)

    # Constraints
    m.c_ec_prod = po.Constraint(m.c, m.y_p, m.x, m.t, rule=c_ec_prod_rule)
    m.c_ec_con = po.Constraint(m.c, m.y_p, m.x, m.t, rule=c_ec_con_rule)


def node_costs(model):
    """
    Defines variables:

    * cost: total costs
    * cost_con: construction costs
    * cost_op_fixed: fixed operation costs
    * cost_op_var: variable operation costs
    * cost_op_fuel: primary resource fuel costs
    * cost_op_rb: secondary resource fuel costs

    """
    m = model.m
    time_res = model.data['_time_res'].to_series()
    weights = model.data['_weights'].to_series()

    cost_getter = utils.cost_getter(model.get_option)
    depreciation_getter = utils.depreciation_getter(model.get_option)
    cost_per_distance_getter = utils.cost_per_distance_getter(model.config_model)

    @utils.memoize
    def _depreciation_rate(y, k):
        return depreciation_getter(y, k)

    @utils.memoize
    def _cost(cost, y, k, x=None):
        return cost_getter(cost, y, k, x=x)

    @utils.memoize
    def _cost_per_distance(cost, y, k, x):
        return cost_per_distance_getter(cost, y, k, x)

    def _check_and_set(cost, y, x, k):
        """
        Ensure that sufficient constraints have been set to allow negative
        costs, where applicable.
        Returns cost if bounds are set, raises error if unset
        """
        e = exceptions.OptionNotSetError

        if y in m.y_trans:
            # Divided by 2 for transmission techs because construction costs
            # are counted at both ends
            unit_cost = (_cost(cost, y, k, x)
                + _cost_per_distance(cost, y, k, x)) / 2
        else:
            unit_cost = _cost(cost, y, k, x)

        if (y, x) in getattr(m, 'c_' + cost).keys() or unit_cost >= 0:
            return unit_cost * getattr(m, cost)[y, x]
        elif unit_cost < 0:
            raise e(cost + '.max must be defined for {}:{} '
                    'as cost is negative'.format(y, x))

    # Variables
    m.cost = po.Var(m.y, m.x, m.k, within=po.Reals)
    m.cost_con = po.Var(m.y, m.x, m.k, within=po.Reals)
    m.cost_op_fixed = po.Var(m.y, m.x, m.k, within=po.Reals)
    m.cost_op_variable = po.Var(m.y, m.x, m.k, within=po.Reals)
    m.cost_op_var = po.Var(m.y, m.x, m.t, m.k, within=po.Reals)
    m.cost_op_fuel = po.Var(m.y, m.x, m.t, m.k, within=po.Reals)
    m.cost_op_rb = po.Var(m.y, m.x, m.t, m.k, within=po.Reals)

    # Constraint rules
    def c_cost_rule(m, y, x, k):
        return (
            m.cost[y, x, k] ==
            m.cost_con[y, x, k] +
            m.cost_op_fixed[y, x, k] +
            m.cost_op_variable[y, x, k]
        )

    def c_cost_con_rule(m, y, x, k):
        if y in m.y_pc:
            cost_s_cap = _check_and_set('s_cap', y, x, k)
        else:
            cost_s_cap = 0

        if y in m.y_def_r:
            cost_r_cap = _check_and_set('r_cap', y, x, k)
            cost_r_area = _check_and_set('r_area', y, x, k)
        else:
            cost_r_cap = 0
            cost_r_area = 0

        cost_e_cap = _check_and_set('e_cap', y, x, k)

        if y in m.y_rb:
            cost_rb_cap = _check_and_set('rb_cap', y, x, k)
        else:
            cost_rb_cap = 0

        return (
            m.cost_con[y, x, k] == _depreciation_rate(y, k) *
            (sum(time_res * weights) / 8760) *
            (cost_s_cap + cost_r_cap + cost_r_area + cost_rb_cap +
             cost_e_cap)
        )

    def c_cost_op_fixed_rule(m, y, x, k):
        if y in m.y:
            if _cost('om_fixed', y, k, x) < 0 and (y, x) not in m.c_e_cap.keys():
                raise exceptions.OptionNotSetError('e_cap.max must be defined '
                        'for {}:{} as `om_fixed` cost is negative'.format(y, x))
            return (m.cost_op_fixed[y, x, k] ==
                    _cost('om_frac', y, k, x) * m.cost_con[y, x, k]
                    + (_cost('om_fixed', y, k, x) * m.e_cap[y, x] *
                       (sum(time_res * weights) / 8760)))
        else:
            return m.cost_op_fixed[y, x, k] == 0

    def c_cost_op_variable_rule(m, y, x, k):
        return (
            m.cost_op_variable[y, x, k] ==
            sum(
                m.cost_op_var[y, x, t, k] +
                m.cost_op_fuel[y, x, t, k] +
                m.cost_op_rb[y, x, t, k]
                for t in m.t
            )
        )

    def c_cost_op_var_rule(m, y, x, t, k):
        # Note: only counting es_prod for operational costs.
        # This should generally be a reasonable assumption to make.
        if y in m.y:
            carrier = model.get_option(y + '.carrier')
            export = m.export[y, x, t] if model.get_option(y + '.export', x=x) else 0
            return (
                m.cost_op_var[y, x, t, k] == weights.loc[t] * (
                get_cost_param(model,'om_var', k, y, x, t) *
                (m.es_prod[carrier, y, x, t] + export) +
                get_cost_param(model, 'export', k, y, x, t) * export)
            )
        else:
            return m.cost_op_var[y, x, t, k] == 0

    def c_cost_op_fuel_rule(m, y, x, t, k):
        r_eff = get_constraint_param(model, 'r_eff', y, x, t)
        if po.value(r_eff) > 0:
            om_fuel = get_cost_param(model,'om_fuel', k, y, x, t)
            # Dividing by r_eff here so we get the actual r used, not the rs
            # moved into storage...
            return (
                m.cost_op_fuel[y, x, t, k] ==
                om_fuel *
                weights.loc[t] *
                (m.rs[y, x, t] / r_eff)
            )
        else: #in case r_eff is zero, to avoid an infinite value for cost_op_fuel
            return m.cost_op_fuel[y, x, t, k] == 0

    def c_cost_op_rb_rule(m, y, x, t, k):
        rb_eff = get_constraint_param(model, 'rb_eff', y, x, t)
        if y in m.y_rb and po.value(rb_eff) > 0:
            return (
                m.cost_op_rb[y, x, t, k] ==
                get_cost_param(model,'om_rb',k,y,x,t) *
                weights.loc[t] *
                (m.rbs[y, x, t] / rb_eff)
            )
        else:
            return m.cost_op_rb[y, x, t, k] == 0


    # Constraints
    m.c_cost = po.Constraint(m.y, m.x, m.k, rule=c_cost_rule)
    m.c_cost_con = po.Constraint(m.y, m.x, m.k, rule=c_cost_con_rule)
    m.c_cost_op_fixed = po.Constraint(m.y, m.x, m.k, rule=c_cost_op_fixed_rule)
    m.c_cost_op_variable = po.Constraint(m.y, m.x, m.k, rule=c_cost_op_variable_rule)
    m.c_cost_op_var = po.Constraint(m.y, m.x, m.t, m.k, rule=c_cost_op_var_rule)
    m.c_cost_op_fuel = po.Constraint(m.y, m.x, m.t, m.k, rule=c_cost_op_fuel_rule)
    m.c_cost_op_rb = po.Constraint(m.y, m.x, m.t, m.k, rule=c_cost_op_rb_rule)


def model_constraints(model):
    m = model.m

    @utils.memoize
    def get_parents(level):
        return list(model._locations[model._locations._level == level].index)

    @utils.memoize
    def get_children(parent, childless_only=True):
        """
        If childless_only is True, only children that have no children
        themselves are returned.

        """
        locations = model._locations
        children = list(locations[locations._within == parent].index)
        if childless_only:  # FIXME childless_only param needs tests
            children = [i for i in children if len(get_children(i)) == 0]
        return children

    # Constraint rules
    def c_system_balance_rule(m, c, x, t):
        # Balacing takes place at top-most (level 0) locations, as well
        # as within any lower-level locations that contain children
        if (model._locations.at[x, '_level'] == 0
                or len(get_children(x)) > 0):
            family = get_children(x) + [x]  # list of children + parent
            balance = (sum(m.es_prod[c, y, xs, t]
                           for xs in family for y in m.y_np)
                       + sum(m.ec_prod[c, y, xs, t]
                             for xs in family for y in m.y_p)
                       + sum(m.es_con[c, y, xs, t]
                             for xs in family for y in m.y_np)
                       + sum(m.ec_con[c, y, xs, t]
                             for xs in family for y in m.y_p))
            if c == 'power':
                return balance == 0
            else:  # e.g. for heat
                return balance >= 0
        else:
            return po.Constraint.NoConstraint

    # Constraints
    m.c_system_balance = po.Constraint(m.c, m.x, m.t,
                                       rule=c_system_balance_rule)
