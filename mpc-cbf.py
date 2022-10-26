import do_mpc
from casadi import *

import config
from plotter import Plotter


class MPC:
    def __init__(self):
        self.sim_time = config.sim_time          # Total simulation time steps
        self.Ts = config.Ts                      # Sampling time
        self.T_horizon = config.T_horizon        # Prediction horizon
        self.x0 = config.x0                      # Initial pose
        self.v_limit = config.v_limit            # Linear velocity limit
        self.omega_limit = config.omega_limit    # Angular velocity limit
        self.R = config.R                        # Controls cost matrix
        self.Q = config.Q                        # State cost matrix
        self.obstacles_on = config.obstacles_on  # Whether to have obstacles
        if self.obstacles_on:
            self.obs = config.obs                # Obstacles
            self.r = config.r                    # Robot radius (for obstacle avoidance)
        self.control_type = config.control_type  # "setpoint" or "traj_tracking"
        if self.control_type == "setpoint":      # Go-to-goal
            self.goal = config.goal              # Robot's goal pose
        self.gamma = config.gamma

        self.model = self.define_model()
        self.mpc = self.define_mpc()
        self.simulator = self.define_simulator()
        self.estimator = do_mpc.estimator.StateFeedback(self.model)
        self.set_init_state()

    def define_model(self):
        """Configures the dynamical model of the system (and part of the objective function)."""

        model_type = 'discrete'
        model = do_mpc.model.Model(model_type)

        # States
        n_states = 3
        _x = model.set_variable(var_type='_x', var_name='x', shape=(n_states, 1))

        # Inputs
        n_controls = 2
        _u = model.set_variable(var_type='_u', var_name='u', shape=(n_controls, 1))

        # State Space matrices
        a = 1e-9  # Small positive constant so system has relative degree 1
        B = SX.zeros(3, 2)
        B[0, 0] = cos(_x[2])
        B[0, 1] = -a*sin(_x[2])
        B[1, 0] = sin(_x[2])
        B[1, 1] = a*cos(_x[2])
        B[2, 1] = 1

        # Set right-hand-side of ODE for all introduced states (_x).
        x_next = _x + B@_u*self.Ts
        model.set_rhs('x', x_next, process_noise=False)  # ToDo Optional noise

        # Optional: Define an expression, which represents the stage and terminal
        # cost of the control problem. This term will be later used as the cost in
        # the MPC formulation and can be used to directly plot the trajectory of
        # the cost of each state.
        model, cost_expr = self.get_cost_expression(model)
        model.set_expression(expr_name='cost', expr=cost_expr)

        # Setup model
        model.setup()

        return model

    def get_cost_expression(self, model):
        """Defines the objective function wrt the state cost depending on the type of control."""
        if self.control_type == "setpoint":  # Go-to-goal
            # Define state error
            X = model.x['x'] - self.goal
        else:                                # Trajectory tracking
            # Set time-varying parameters for the objective function
            model.set_variable('_tvp', 'x_set_point')
            model.set_variable('_tvp', 'y_set_point')
            # Define state error
            theta_des = np.arctan2(model.x['x', 1] - model.tvp['y_set_point'], model.x['x', 0] - model.tvp['x_set_point'])
            X = SX.zeros(3, 1)
            X[0] = model.x['x', 0] - model.tvp['x_set_point']
            X[1] = model.x['x', 1] - model.tvp['y_set_point']
            X[2] = model.x['x', 2] - theta_des

        cost_expression = transpose(X)@self.Q@X
        return model, cost_expression

    def define_mpc(self):
        """Configures the mpc controller."""

        mpc = do_mpc.controller.MPC(self.model)

        # Set parameters
        setup_mpc = {'n_robust': 0,  # Robust horizon
                     'n_horizon': self.T_horizon,
                     't_step': self.Ts,
                     'state_discretization': 'discrete',
                     'store_full_solution': True
                     }
        mpc.set_param(**setup_mpc)

        # Configure objective function
        mterm = self.model.aux['cost']  # Terminal cost
        lterm = self.model.aux['cost']  # Stage cost
        mpc.set_objective(mterm=mterm, lterm=lterm)
        mpc.set_rterm(u=self.R)         # Input penalty (R diagonal matrix in objective fun)

        # State and input bounds
        max_u = np.array([self.v_limit, self.omega_limit])
        mpc.bounds['lower', '_u', 'u'] = -max_u
        mpc.bounds['upper', '_u', 'u'] = max_u

        # Optional: Add obstacle avoidance constraints
        # if self.obstacles_on:
        #     i = 0
        #     for x_obs, y_obs, r_obs in self.obs:
        #         obs_avoid = - (self.model.x['x'][0] - x_obs)**2 \
        #                     - (self.model.x['x'][1] - y_obs)**2 \
        #                     + (self.r + r_obs)**2
        #         mpc.set_nl_cons('obstacle_constraint'+str(i), obs_avoid, ub=0)
        #         i += 1

        # Add CBF constraints
        a = 1e-9  # Small positive constant so system has relative degree 1
        B = SX.zeros(3, 2)
        B[0, 0] = cos(self.model.x['x'][2])
        B[0, 1] = -a*sin(self.model.x['x'][2])
        B[1, 0] = sin(self.model.x['x'][2])
        B[1, 1] = a*cos(self.model.x['x'][2])
        B[2, 1] = 1
        
        x_k1 = self.model.x['x'] + B@self.model.u['u']*self.Ts
        h_k1 = self.h(x_k1)
        h_k = self.h(self.model.x['x'])
        cbc = -h_k1 + (1-self.gamma)*h_k
        mpc.set_nl_cons('cbf_constraint', cbc, ub=0)

        # Define time-varying parameters for the objective function, if doing trajectory tracking
        if self.control_type == "traj_tracking":
            mpc = self.set_tvp_for_mpc(mpc)

        mpc.setup()

        return mpc

    def h(self, x):
        """Control Barrier Function."""
        # h = []
        # for x_obs, y_obs, r_obs in self.obs:
        #     h.append((x[0] - x_obs)**2 + (x[1] - y_obs)**2 + (self.r + r_obs)**2)

        x_obs, y_obs, r_obs = self.obs[0]
        h = (x[0] - x_obs)**2 + (x[1] - y_obs)**2 - (self.r + r_obs)**2
        return h


    @staticmethod
    def set_tvp_for_mpc(mpc):
        """Sets the trajectory to be followed for trajectory tracking."""
        tvp_struct_mpc = mpc.get_tvp_template()

        def tvp_fun_mpc(t_now):
            # Trajectory to follow
            if config.trajectory == "circular":
                x_traj = config.A*cos(config.w*t_now)
                y_traj = config.A*sin(config.w*t_now)

            tvp_struct_mpc['_tvp', :, 'x_set_point'] = x_traj
            tvp_struct_mpc['_tvp', :, 'y_set_point'] = y_traj
            return tvp_struct_mpc

        mpc.set_tvp_fun(tvp_fun_mpc)
        return mpc

    def define_simulator(self):
        """Configures the simulator."""
        simulator = do_mpc.simulator.Simulator(self.model)
        simulator.set_param(t_step=self.Ts)

        if self.control_type == "traj_tracking":
            tvp_template = simulator.get_tvp_template()

            def tvp_fun(t_now):
                return tvp_template
            simulator.set_tvp_fun(tvp_fun)

        simulator.setup()
        return simulator

    def set_init_state(self):
        """Sets the initial state in all components."""
        self.mpc.x0 = self.x0
        self.simulator.x0 = self.x0
        self.estimator.x0 = self.x0
        self.mpc.set_initial_guess()

    def run_simulation(self):
        """Runs a closed-loop control simulation."""
        x0 = self.x0
        for k in range(self.sim_time):
            u0 = self.mpc.make_step(x0)
            y_next = self.simulator.make_step(u0)
            x0 = self.estimator.make_step(y_next)


def main():
    """ """
    np.random.seed(99)

    controller = MPC()           # Define model, controller, simulator and estimator
    controller.run_simulation()  # Closed-loop control simulation

    plotter = Plotter(controller.mpc)
    # plotter.plot_results()
    # plotter.plot_predictions()
    plotter.plot_path()
    # plotter.create_animation()


if __name__ == '__main__':
    main()