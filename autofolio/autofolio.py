import logging
import functools
import traceback
import random
from itertools import tee
import pickle

import numpy as np
import pandas as pd

from ConfigSpace.configuration_space import Configuration, \
    ConfigurationSpace
from ConfigSpace.hyperparameters import CategoricalHyperparameter, \
    UniformFloatHyperparameter, UniformIntegerHyperparameter

# SMAC3
from smac.tae.execute_func import ExecuteTAFuncDict
from smac.scenario.scenario import Scenario
from smac.stats.stats import Stats as AC_Stats
from smac.facade.smac_facade import SMAC

from autofolio.io.cmd import CMDParser
from aslib_scenario.aslib_scenario import ASlibScenario

# feature preprocessing
from autofolio.feature_preprocessing.pca import PCAWrapper
from autofolio.feature_preprocessing.missing_values import ImputerWrapper
from autofolio.feature_preprocessing.feature_group_filtering import FeatureGroupFiltering
from autofolio.feature_preprocessing.standardscaler import StandardScalerWrapper

# presolving
from autofolio.pre_solving.aspeed_schedule import Aspeed

# classifiers
from autofolio.selector.classifiers.random_forest import RandomForest
from autofolio.selector.classifiers.xgboost import XGBoost

# regressors
from autofolio.selector.regressors.random_forest import RandomForestRegressor

# selectors
from autofolio.selector.pairwise_classification import PairwiseClassifier
from autofolio.selector.pairwise_regression import PairwiseRegression

# validation
from autofolio.validation.validate import Validator, Stats

__author__ = "Marius Lindauer"
__license__ = "BSD"
__version__ = "2.0.0"


class AutoFolio(object):

    def __init__(self, random_seed: int=12345):
        ''' Constructor 

            Arguments
            ---------
            random_seed: int
                random seed for numpy and random packages
        '''

        np.random.seed(random_seed)  # fix seed
        random.seed(random_seed)

        # I don't know the reason, but without an initial print with
        # logging.info we don't get any output
        logging.info("Init AutoFolio")
        self._root_logger = logging.getLogger()
        self.logger = logging.getLogger("AutoFolio")
        self.cs = None

        self.overwrite_args = None

    def run_cli(self):
        '''
            main method of AutoFolio based on command line interface
        '''

        cmd_parser = CMDParser()
        args_, self.overwrite_args = cmd_parser.parse()

        self._root_logger.setLevel(args_.verbose)

        if args_.load:
            self.read_model_and_predict(
                model_fn=args_.load, feature_vec=list(map(float, args_.feature_vec)))
        else:

            scenario = ASlibScenario()
            if args_.scenario:
                scenario.read_scenario(args_.scenario)
            elif args_.performance_csv and args_.feature_csv:
                scenario.read_from_csv(perf_fn=args_.performance_csv,
                                       feat_fn=args_.feature_csv,
                                       objective=args_.objective,
                                       runtime_cutoff=args_.runtime_cutoff,
                                       maximize=args_.maximize,
                                       cv_fn=args_.cv_csv)
            else:
                raise ValueError("Missing inputs to read scenario data.")

            self.cs = self.get_cs(scenario)

            if args_.tune:
                config = self.get_tuned_config(scenario)
            else:
                config = self.cs.get_default_configuration()
            self.logger.debug(config)

            if args_.save:
                feature_pre_pipeline, pre_solver, selector = self.fit(
                    scenario=scenario, config=config)
                self._save_model(
                    args_.save, scenario, feature_pre_pipeline, pre_solver, selector, config)
            else:
                self.run_cv(config=config, scenario=scenario, folds=scenario.cv_data.max().max())

    def _save_model(self, out_fn: str, scenario: ASlibScenario, feature_pre_pipeline: list, pre_solver: Aspeed, selector, config: Configuration):
        '''
            save all pipeline objects for predictions

            Arguments
            ---------
            out_fn: str
                filename of output file
            scenario: AslibScenario
                ASlib scenario with all the data
            feature_pre_pipeline: list
                list of preprocessing objects
            pre_solver: Aspeed
                aspeed object with pre-solving schedule
            selector: autofolio.selector.*
                fitted selector object
            config: Configuration
                parameter setting configuration
        '''
        scenario.logger = None
        for fpp in feature_pre_pipeline:
            fpp.logger = None
        if pre_solver:
            pre_solver.logger = None
        selector.logger = None
        model = [scenario, feature_pre_pipeline, pre_solver, selector, config]
        with open(out_fn, "bw") as fp:
            pickle.dump(model, fp)

    def read_model_and_predict(self, model_fn: str, feature_vec: list):
        '''
            reads saved model from disk and predicts the selected algorithm schedule for a given feature vector

            Arguments
            --------
            model_fn: str
                file name of saved model
            feature_vec: list
                instance feature vector as a list of floats 
        '''
        with open(model_fn, "br") as fp:
            scenario, feature_pre_pipeline, pre_solver, selector, config = pickle.load(
                fp)

        for fpp in feature_pre_pipeline:
            fpp.logger = logging.getLogger("Feature Preprocessing")
        if pre_solver:
            pre_solver.logger = logging.getLogger("Aspeed PreSolving")
        selector.logger = logging.getLogger("Selector")

        # saved scenario is adapted to given feature vector
        feature_vec = np.array([feature_vec])
        scenario.feature_data = pd.DataFrame(
            feature_vec, index=["pseudo_instance"], columns=scenario.feature_names)
        scenario.instances = ["pseudo_instance"]
        pred = self.predict(scenario=scenario, config=config,
                            feature_pre_pipeline=feature_pre_pipeline, pre_solver=pre_solver, selector=selector)

        print("Selected Schedule [(algorithm, budget)]: %s" % (
            pred["pseudo_instance"]))

    def get_cs(self, scenario: ASlibScenario):
        '''
            returns the parameter configuration space of AutoFolio
            (based on the automl config space: https://github.com/automl/ConfigSpace)

            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
        '''

        self.cs = ConfigurationSpace()

        # add feature steps as binary parameters
        for fs in scenario.feature_steps:
            fs_param = CategoricalHyperparameter(name="fgroup_%s" % (
                fs), choices=[True, False], default=fs in scenario.feature_steps_default)
            self.cs.add_hyperparameter(fs_param)

        # preprocessing
        PCAWrapper.add_params(self.cs)
        ImputerWrapper.add_params(self.cs)
        StandardScalerWrapper.add_params(self.cs)

        # Pre-Solving
        if scenario.performance_type[0] == "runtime":
            Aspeed.add_params(
                cs=self.cs, cutoff=scenario.algorithm_cutoff_time)

        # classifiers
        RandomForest.add_params(self.cs)
        XGBoost.add_params(self.cs)
       
        # regressors
        RandomForestRegressor.add_params(self.cs)

        # selectors
        PairwiseClassifier.add_params(self.cs)
        PairwiseRegression.add_params(self.cs)       

        self.logger.debug(self.cs)

        return self.cs

    def get_tuned_config(self, scenario: ASlibScenario):
        '''
            uses SMAC3 to determine a well-performing configuration in the configuration space self.cs on the given scenario

            Arguments
            ---------
            scenario: ASlibScenario
                ASlib Scenario at hand

            Returns
            -------
            Configuration
                best incumbent configuration found by SMAC
        '''

        taf = ExecuteTAFuncDict(functools.partial(self.called_by_smac, scenario=scenario))

        ac_scenario = Scenario({"run_obj": "quality",  # we optimize quality
                                # at most 10 function evaluations
                                "runcount-limit": 1000,
                                "cs": self.cs,  # configuration space
                                "deterministic": "true",
                                "instances": [[i] for i in range(1,scenario.cv_data.max().max() +1)]
                                })

        # necessary to use stats options related to scenario information
        AC_Stats.scenario = ac_scenario

        # Optimize
        self.logger.info(
            ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        self.logger.info("Start Configuration")
        self.logger.info(
            ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        smac = SMAC(scenario=ac_scenario, tae_runner=taf,
                    rng=np.random.RandomState(42))
        incumbent = smac.optimize()

        self.logger.info("Final Incumbent: %s" % (incumbent))

        return incumbent

    def called_by_smac(self, config: Configuration, scenario: ASlibScenario, instance=None, seed:int=1):
        '''
            run a cross fold validation based on the given data from cv.arff

            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
            config: Configuration
                parameter configuration to use for preprocessing
            folds: int
                number of cv-splits
            seed: int
                random seed (not used)
                
            Returns
            -------
            float: average performance
        '''
        
        if instance is None:
            perf = self.run_cv(config=config, scenario=scenario)
        else:
            try:
                stats = self.run_fold(config=config, scenario=scenario, fold=instance)
                perf = stats.show()
            except ValueError:
                if scenario.performance_type[0] == "runtime":
                    perf = scenario.algorithm_cutoff_time * 10
                else:
                    # try to impute a worst case perf
                    perf = scenario.performance_data.max().max()
                
        if scenario.maximize[0]:
            perf *= -1
        
        return perf

    def run_cv(self, config: Configuration, scenario: ASlibScenario, folds:int=10):
        '''
            run a cross fold validation based on the given data from cv.arff

            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
            config: Configuration
                parameter configuration to use for preprocessing
            folds: int
                number of cv-splits
            seed: int
                random seed (not used)
        '''
        #TODO: use seed and instance in an appropriate way
        try:
            if scenario.performance_type[0] == "runtime":
                cv_stat = Stats(runtime_cutoff=scenario.algorithm_cutoff_time)
            else:
                cv_stat = Stats(runtime_cutoff=0)
            for i in range(1, folds + 1):
                self.logger.info("CV-Iteration: %d" % (i))
                stats = self.run_fold(config=config,
                                      scenario=scenario,
                                      fold=i)
                cv_stat.merge(stat=stats)

            self.logger.info(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
            self.logger.info("CV Stats")
            par10 = cv_stat.show()
        except ValueError:
            traceback.print_exc()
            par10 = scenario.algorithm_cutoff_time * 10

        if scenario.maximize[0]:
            par10 *= -1

        return par10

    def run_fold(self, config: Configuration, scenario:ASlibScenario, fold:int):
        '''
            run a given fold of cross validation
            
            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
            config: Configuration
                parameter configuration to use for preprocessing
            fold: int
                fold id
                
            Returns
            -------
            Stats()
                
        '''
        self.logger.info("CV-Iteration: %d" % (fold))
        
        test_scenario, training_scenario = scenario.get_split(indx=fold)

        feature_pre_pipeline, pre_solver, selector = self.fit(
            scenario=training_scenario, config=config)

        schedules = self.predict(
            test_scenario, config, feature_pre_pipeline, pre_solver, selector)

        val = Validator()
        if scenario.performance_type[0] == "runtime":
            stats = val.validate_runtime(
                schedules=schedules, test_scenario=test_scenario)
        elif scenario.performance_type[0] == "solution_quality":
            stats = val.validate_quality(
                schedules=schedules, test_scenario=test_scenario)
        else:
            raise ValueError("Unknown: %s" %(performance_type[0]))
        
        return stats

    def fit(self, scenario: ASlibScenario, config: Configuration):
        '''
            fit AutoFolio on given ASlib Scenario

            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
            config: Configuration
                parameter configuration to use for preprocessing

            Returns
            -------
                list of fitted feature preproccessing objects
                pre-solving object
                fitted selector
        '''
        self.logger.info("Given Configuration: %s" % (config))

        if self.overwrite_args:
            config = self._overwrite_configuration(
                config=config, overwrite_args=self.overwrite_args)
            self.logger.info("Overwritten Configuration: %s" % (config))

        scenario, feature_pre_pipeline = self.fit_transform_feature_preprocessing(
            scenario, config)

        pre_solver = self.fit_pre_solving(scenario, config)

        selector = self.fit_selector(scenario, config)

        return feature_pre_pipeline, pre_solver, selector

    def _overwrite_configuration(self, config: Configuration, overwrite_args: list):
        '''
            overwrites a given configuration with some new settings

            Arguments
            ---------
            config: Configuration
                initial configuration to be adapted
            overwrite_args: list
                new parameter settings as a list of strings

            Returns
            -------
            Configuration
        '''

        def pairwise(iterable):
            a, b = tee(iterable)
            next(b, None)
            return zip(a, b)

        dict_conf = config.get_dictionary()
        for param, value in pairwise(overwrite_args):
            try:
                ok = self.cs.get_hyperparameter(param)
            except KeyError:
                ok = None
            if ok is not None:
                if type(self.cs.get_hyperparameter(param)) is UniformIntegerHyperparameter:
                    dict_conf[param] = int(value)
                elif type(self.cs.get_hyperparameter(param)) is UniformFloatHyperparameter:
                    dict_conf[param] = float(value)
                elif value == "True":
                    dict_conf[param] = True
                elif value == "False":
                    dict_conf[param] = False
                else:
                    dict_conf[param] = value
            else:
                self.logger.warn(
                    "Unknown given parameter: %s %s" % (param, value))
        config = Configuration(self.cs, values=dict_conf, allow_inactive_with_values=True)

        return config

    def fit_transform_feature_preprocessing(self, scenario: ASlibScenario, config: Configuration):
        '''
            performs feature preprocessing on a given ASlib scenario wrt to a given configuration

            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
            config: Configuration
                parameter configuration to use for preprocessing

            Returns
            -------
                list of fitted feature preproccessing objects
        '''

        pipeline = []
        fgf = FeatureGroupFiltering()
        scenario = fgf.fit_transform(scenario, config)

        imputer = ImputerWrapper()
        scenario = imputer.fit_transform(scenario, config)

        scaler = StandardScalerWrapper()
        scenario = scaler.fit_transform(scenario, config)

        pca = PCAWrapper()
        scenario = pca.fit_transform(scenario, config)

        return scenario, [fgf, imputer, scaler, pca]

    def fit_pre_solving(self, scenario: ASlibScenario, config: Configuration):
        '''
            fits an pre-solving schedule using Aspeed [Hoos et al, 2015 TPLP) 

            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
            config: Configuration
                parameter configuration to use for preprocessing

            Returns
            -------
            instance of Aspeed() with a fitted pre-solving schedule if performance_type of scenario is runtime; else None
        '''
        if scenario.performance_type[0] == "runtime":
            aspeed = Aspeed()
            aspeed.fit(scenario=scenario, config=config)
            return aspeed
        else:
            return None

    def fit_selector(self, scenario: ASlibScenario, config: Configuration):
        '''
            fits an algorithm selector for a given scenario wrt a given configuration

            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
            config: Configuration
                parameter configuration
        '''

        if config.get("selector") == "PairwiseClassifier":

            clf_class = None
            if config.get("classifier") == "RandomForest":
                clf_class = RandomForest
            if config.get("classifier") == "XGBoost":
                clf_class = XGBoost

            selector = PairwiseClassifier(classifier_class=clf_class)
            selector.fit(scenario=scenario, config=config)

        if config.get("selector") == "PairwiseRegressor":
            reg_class = None
            if config.get("regressor") == "RandomForestRegressor":
                reg_class = RandomForestRegressor
                
            selector = PairwiseRegression(regressor_class=reg_class)
            selector.fit(scenario=scenario, config=config)

        return selector

    def predict(self, scenario: ASlibScenario, config: Configuration, feature_pre_pipeline: list, pre_solver: Aspeed, selector):
        '''
            predicts algorithm schedules wrt a given config
            and given pipelines

            Arguments
            ---------
            scenario: aslib_scenario.aslib_scenario.ASlibScenario
                aslib scenario at hand
            config: Configuration
                parameter configuration
            feature_pre_pipeline: list
                list of fitted feature preprocessors
            pre_solver: Aspeed
                pre solver object with a saved static schedule
            selector: autofolio.selector.*
                fitted selector object
        '''

        self.logger.info("Predict on Test")
        for f_pre in feature_pre_pipeline:
            scenario = f_pre.transform(scenario)

        if pre_solver:
            pre_solving_schedule = pre_solver.predict(scenario=scenario)
        else:
            pre_solving_schedule = {}

        pred_schedules = selector.predict(scenario=scenario)

        # combine schedules
        if pre_solving_schedule:
            return dict((inst, pre_solving_schedule.get(inst, []) + schedule) for inst, schedule in pred_schedules.items())
        else:
            return pred_schedules
