from casadi import MX, inf, MXFunction, nlpIn, nlpOut, NlpSolver, SX
from casadi import symvar, substitute, SXFunction
from casadi.tools import struct, struct_MX, struct_symMX, entry, struct_symSX
from spline import BSpline
from itertools import groupby
import time
import numpy as np
import copy


def evalf(fun, x):
    x = x if isinstance(x, list) else [x]
    return fun(x)


class OptiFather:

    def __init__(self, children=[]):
        self.children = {}
        self.symbol_dict = {}
        for child in children:
            self.add(child)

    def add(self, children):
        children = children if isinstance(children, list) else [children]
        for child in children:
            child.father = self
            self.children.update({child.label: child})

    def add_to_dict(self, symbol, child, name):
        for sym in symvar(symbol):
            self.symbol_dict[sym.getName()] = [child, name]

    # ========================================================================
    # Problem composition
    # ========================================================================

    def construct_problem(self, options, build=None):
        self.compose_dictionary()
        self.translate_symbols()
        variables = self.construct_variables()
        parameters = self.construct_parameters()
        constraints, lb, ub = self.construct_constraints(variables, parameters)
        objective = self.construct_objective(variables, parameters)
        if build is None:
            problem, buildtime = self.create_nlp(variables, parameters,
                                             objective, constraints, options)
        else:
            problem = build
            buildtime = 0.
        self.init_variables()
        return problem, buildtime

    def compose_dictionary(self):
        for label, child in self.children.items():
            self.symbol_dict.update(child.symbol_dict)

    def translate_symbols(self):
        for label, child in self.children.items():
            for name, symbol in child._symbols.items():
                sym_def = []
                for _label, _child in self.children.items():
                    if (name in _child._variables or
                            name in _child._parameters):
                        sym_def.append(_child)
                if len(sym_def) > 1:
                    raise ValueError('Symbol %s, defined in %s, is defined'
                                     ' multiple times as parameter or'
                                     ' variable by %s!' %
                                     (name, label,
                                      ','.join([sd.label for sd in sym_def])))
                elif len(sym_def) == 0:
                    raise ValueError('Symbol %s, defined in %s, is not defined'
                                     'as parameter or variable by any object' %
                                     (name, label))
                else:
                    self.add_to_dict(symbol, sym_def[0], name)

    def construct_variables(self):
        entries = []
        for label, child in self.children.items():
            entries_child = []
            for name, var in child._variables.items():
                entries_child.append(entry(name, shape=var.shape))
            entries.append(entry(label, struct=struct(entries_child)))
        self._var_struct = struct(entries)
        return struct_symMX(self._var_struct)

    def construct_parameters(self):
        entries = []
        for label, child in self.children.items():
            entries_child = []
            for name, par in child._parameters.items():
                entries_child.append(entry(name, shape=par.shape))
            entries.append(entry(label, struct=struct(entries_child)))
        self._par_struct = struct(entries)
        return struct_symMX(self._par_struct)

    def construct_constraints(self, variables, parameters):
        entries = []
        for child in self.children.values():
            for name, constraint in child._constraints.items():
                expression = self._substitute_symbols(
                    constraint[0], variables, parameters)
                entries.append(entry(child._add_label(name), expr=expression))
        constraints = struct_MX(entries)
        self._lb, self._ub = constraints(0), constraints(0)
        self._constraint_shutdown = {}
        for child in self.children.values():
            for name, constraint in child._constraints.items():
                self._lb[child._add_label(name)] = constraint[1]
                self._ub[child._add_label(name)] = constraint[2]
                if constraint[3]:
                    self._constraint_shutdown[
                        child._add_label(name)] = constraint[3]
        return constraints, self._lb, self._ub

    def construct_objective(self, variables, parameters):
        objective = 0.
        for child in self.children.values():
            objective += self._substitute_symbols(
                child._objective, variables, parameters)
        return objective

    def _substitute_symbols(self, expr, variables, parameters):
        if isinstance(expr, (int, float)):
            return expr
        for sym in symvar(expr):
            [child, name] = self.symbol_dict[sym.getName()]
            if name in child._variables:
                expr = substitute(expr, sym, variables[child.label, name])
            elif name in child._parameters:
                expr = substitute(expr, sym, parameters[child.label, name])
        return expr

    def _evaluate_symbols(self, expression, variables, parameters):
        symbols = symvar(expression)
        f = MXFunction(symbols, [expression])
        f.init()
        f_in = []
        for sym in symbols:
            [child, name] = self.symbol_dict[sym.getName()]
            if name in child._variables:
                f_in.append(variables[child.label, name])
            elif name in child._parameters:
                f_in.append(parameters[child.label, name])
        return evalf(f, f_in)[0]

    # ========================================================================
    # Methods related to c code generation
    # ========================================================================

    def create_nlp(self, var, par, obj, con, options):
        jit = options['codegen']
        if options['verbose'] >= 2:
            print 'Building nlp ... ',
            if jit['jit']:
                print('[jit compilation with flags %s]' %
                      (','.join(jit['jit_options']['flags']))),
        t0 = time.time()
        nlp = MXFunction('nlp', nlpIn(x=var, p=par), nlpOut(f=obj, g=con))
        solver = NlpSolver('solver', 'ipopt', nlp, options['solver'])
        grad_f, jac_g = nlp.gradient('x', 'f'), nlp.jacobian('x', 'g')
        hess_lag = solver.hessLag()
        var, par = struct_symSX(var), struct_symSX(par)
        nlp = SXFunction('nlp', [var, par], nlp([var, par]), jit)
        grad_f = SXFunction('grad_f', [var, par], grad_f([var, par]), jit)
        jac_g = SXFunction('jac_g', [var, par], jac_g([var, par]), jit)
        lam_f, lam_g = SX.sym('lam_f'), SX.sym('lam_g', con.size)
        hess_lag = SXFunction('hess_lag', [var, par, lam_f, lam_g],
                              hess_lag([var, par, lam_f, lam_g]), jit)
        options['solver'].update({'grad_f': grad_f, 'jac_g': jac_g,
                                  'hess_lag': hess_lag})
        problem = NlpSolver('solver', 'ipopt', nlp, options['solver'])
        t1 = time.time()
        if options['verbose'] >= 2:
            print 'in %5f s' % (t1-t0)
        return problem, (t1-t0)

    def create_function(self, name, inp, out, options):
        jit = options['codegen']
        if options['verbose'] >= 2:
            print 'Building function %s ... ' % name,
            if jit['jit']:
                print('[jit compilation with flags %s]' %
                      (','.join(jit['jit_options']['flags']))),
        t0 = time.time()
        fun = SXFunction(name, inp, out, jit)
        t1 = time.time()
        if options['verbose'] >= 2:
            print 'in %5f s' % (t1-t0)
        return fun, (t1-t0)

    # ========================================================================
    # Problem evaluation
    # ========================================================================

    def update_bounds(self, current_time):
        lb, ub = copy.deepcopy(self._lb), copy.deepcopy(self._ub)
        for name, shutdown in self._constraint_shutdown.items():
            if shutdown(current_time):
                lb[name], ub[name] = -inf, +inf
        return lb, ub

    def init_variables(self):
        variables = self._var_struct(0.)
        for label, child in self.children.items():
            var = child.init_variables()
            for name, v in var.items():
                variables[label, name] = v
        self._var_result = variables

    def set_variables(self, variables):
        self._var_result = self._var_struct(variables)

    def set_parameters(self, time):
        self._par = self._par_struct(0.)
        for label, child in self.children.items():
            par = child.set_parameters(time)
            for name, p in par.items():
                self._par[label, name] = p
        return self._par

    def get_variables(self, label=None, name=None):
        if label is None:
            return self._var_result
        if name is None:
            return self._var_result.prefix(name)
        else:
            return self._var_result[label, name].toArray()

    def get_parameters(self, label=None, name=None):
        if label is None:
            return self._par_result
        if name is None:
            return self._par_result.prefix(name)
        else:
            return self._par_result[label, name].toArray()

    def get_constraint(self, label, name):
        return self._evaluate_symbols(self.children[label]._constraints[name][0],
                                      self._var_result, self._par_result)

    def get_objective(self, label, name):
        return self._evaluate_symbols(self.children[label]._objective,
                                      self._var_result, self._par_result)

    # ========================================================================
    # Variable manipulation
    # ========================================================================

    def transform_spline_variables(self, transformation):
        for label, child in self.children.items():
            for name, basis in child._splines.items():
                if name in child._variables:
                    self._var_result[label, name] = transformation(
                        self._var_result[label, name],
                        basis.knots, basis.degree)


class OptiChild:
    _labels = []

    def __init__(self, label):
        self.label = OptiChild._make_label(label)
        self._variables = {}
        self._parameters = {}
        self._symbols = {}
        self._splines = {}
        self._constraints = {}
        self._objective = 0.
        self.symbol_dict = {}
        self._constraint_cnt = 0

    def __str__(self):
        return self.label

    __repr__ = __str__

    def add_to_dict(self, symbol, name):
        for sym in symvar(symbol):
            if sym in self.symbol_dict:
                raise ValueError('Symbol already added for %s' % self.label)
            self.symbol_dict[sym.getName()] = [self, name]

    def _add_label(self, name):
        return name + '_' + self.label

    @classmethod
    def _make_label(cls, label):
        label_split = [''.join(g) for _, g in groupby(label, str.isalpha)]
        index = label_split[-1]
        rest = ''.join(label_split[:-1])
        if index.isdigit():
            if label in cls._labels:
                return cls._make_label(rest+str(int(index)+1))
            else:
                cls._labels.append(label)
                return label
        else:
            return cls._make_label(label+str(0))

    # ========================================================================
    # Definition of symbols, variables, parameters, constraints, objective
    # ========================================================================

    def define_symbol(self, name, size0=1, size1=1):
        return self._define_mx(name, size0, size1, self._symbols)

    def define_variable(self, name, size0=1, size1=1):
        return self._define_mx(name, size0, size1, self._variables)

    def define_parameter(self, name, size0=1, size1=1):
        return self._define_mx(name, size0, size1, self._parameters)

    def define_spline_symbol(self, name, size0=1, size1=1, **kwargs):
        basis = kwargs['basis'] if 'basis' in kwargs else self.basis
        return self._define_mx_spline(name, size0, size1, self._symbols, basis)

    def define_spline_variable(self, name, size0=1, size1=1, **kwargs):
        basis = kwargs['basis'] if 'basis' in kwargs else self.basis
        return self._define_mx_spline(name, size0, size1,
                                      self._variables, basis)

    def define_spline_parameter(self, name, size0=1, size1=1, **kwargs):
        basis = kwargs['basis'] if 'basis' in kwargs else self.basis
        return self._define_mx_spline(name, size0, size1,
                                      self._parameters, basis)

    def _define_mx(self, name, size0, size1, dictionary):
        symbol_name = self._add_label(name)
        dictionary[name] = MX.sym(symbol_name, size0, size1)
        self.add_to_dict(dictionary[name], name)
        return dictionary[name]

    def _define_mx_spline(self, name, size0, size1, dictionary, basis):
        if size1 > 1:
            return [self._define_mx_spline(name+str(l), size0,
                                           1, dictionary, basis)
                    for l in range(size1)]
        else:
            coeffs = self._define_mx(name, len(basis), size0, dictionary)
            self._splines[name] = basis
            return [BSpline(basis, coeffs[:, k]) for k in range(size0)]

    def define_constraint(self, expr, lb, ub, shutdown=False, name=None):
        if isinstance(expr, (float, int)):
            return
        if name is None:
            name = 'c'+str(self._constraint_cnt)
            self._constraint_cnt += 1
        if name in self._constraints:
            raise ValueError('Name %s already used for constraint!' % (name))
        if isinstance(expr, BSpline):
            self._constraints[name] = (
                expr.coeffs, lb*np.ones(expr.coeffs.shape[0]),
                ub*np.ones(expr.coeffs.shape[0]), shutdown)
        else:
            self._constraints[name] = (expr, lb, ub, shutdown)

    def define_objective(self, expr):
        self._objective = expr

    def get_variable(self, name, **kwargs):
        if 'solution' in kwargs and kwargs['solution']:
            return self.father.get_variables(self.label, name)
        if name in self._splines and not('spline' in kwargs and not kwargs['spline']):
            basis = self._splines[name]
            coeffs = self._variables[name]
            return [BSpline(basis, coeffs[:, k]) for k in range(coeffs.shape[1])]
        else:
            return self._variables[name]

    def get_parameter(self, name, **kwargs):
        if 'solution' in kwargs and kwargs['solution']:
            return self.father.get_parameters(self.label, name)
        if name in self._splines and not('spline' in kwargs and not kwargs['spline']):
            basis = self._splines[name]
            coeffs = self._parameters[name]
            return [BSpline(basis, coeffs[:, k]) for k in range(coeffs.shape[1])]
        else:
            return self._parameters[name]

    def get_constraint(self, name, solution=None):
        if solution:
            return self.father.get_constraint(self.label, name)
        else:
            return self._constraints[name][0]

    def get_objective(self, solution=None):
        if solution:
            return self.father.get_objective(self.label)
        else:
            return self._objective

    # ========================================================================
    # Methods required to override
    # ========================================================================

    def init_variables(self):
        return {}

    def set_parameters(self, time):
        return {}
