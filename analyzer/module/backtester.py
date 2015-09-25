'''
Created on Dec 3, 2011

@author: ppa
'''
import os
import sys
import traceback
import logging
import logging.config


from analyzer.backtest.tick_subscriber.strategies.strategy_factory import StrategyFactory
from analyzer.backtest.trading_center import TradingCenter
from analyzer.backtest.tick_feeder import TickFeeder
from analyzer.backtest.trading_engine import TradingEngine
from analyzer.backtest.account_manager import AccountManager
from analyzer.ufConfig.pyConfig import PyConfig
from analyzerdam.DAMFactory import DAMFactory
from analyzer.backtest.stateSaver.stateSaverFactory import StateSaverFactory
from analyzer.backtest.metric import MetricManager
from analyzer.backtest.index_helper import IndexHelper
from analyzer.backtest.history import History
from analyzer.backtest.constant import (
    CONF_ULTRAFINANCE_SECTION,
    CONF_TRADE_TYPE,
    CONF_INIT_CASH,
    CONF_START_TRADE_DATE,
    CONF_END_TRADE_DATE,
    CONF_SYMBOL_FILE,
    CONF_INDEX,
    CONF_INPUT_DAM,
    CONF_INPUT_DB,
    CONF_SAVER,
    CONF_OUTPUT_DB_PREFIX,
    CONF_STRATEGY_NAME
)
from analyzer.backtest.metric import BasicMetric

from threading import Thread

LOG = logging.getLogger()


class BackTester(object):
    ''' back testing '''

    def __init__(self, configFile, startTickDate=0, startTradeDate=0, endTradeDate=None, cash=150000, symbolLists=None):
        LOG.debug("Loading config from %s" % configFile)
        self.__config = PyConfig()
        self.__config.setSource(configFile)

        self.__cash = cash
        self.__mCalculator = MetricManager()
        self.__symbolLists = symbolLists
        self.__accounts = []
        self.__startTickDate = startTickDate
        self.__startTradeDate = startTradeDate
        self.__endTradeDate = endTradeDate
        self.__firstSaver = None

    @property
    def trade_type(self):
        return self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_TRADE_TYPE)

    def setup(self):
        ''' setup '''
        self.__config.override(CONF_ULTRAFINANCE_SECTION, CONF_INIT_CASH, self.__cash)
        self.__config.override(CONF_ULTRAFINANCE_SECTION, CONF_START_TRADE_DATE, self.__startTradeDate)
        self.__config.override(CONF_ULTRAFINANCE_SECTION, CONF_END_TRADE_DATE, self.__endTradeDate)
        self._setupLog()
        LOG.debug(self.__symbolLists)
        if not self.__symbolLists:
            self._loadSymbols()

    def _setupLog(self):
        ''' setup logging '''
        if self.__config.getSection("loggers"):
            logging.config.fileConfig(self.__config.getFullPath())

    def _runOneTest(self, symbols):
        ''' run one test '''
        LOG.debug("Running backtest for %s" % symbols)
        runner = TestRunner(self.__config, self.__mCalculator, self.__accounts, symbols, self.__startTickDate, self.__endTradeDate, self.__cash, self.trade_type)
        runner.runTest()

    def _loadSymbols(self):
        ''' find symbols'''
        symbolFile = self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_SYMBOL_FILE)
        assert symbolFile is not None, "%s is required in config file" % CONF_SYMBOL_FILE

        LOG.info("loading symbols from %s" % os.path.join(self.__config.getDir(), symbolFile))
        if not self.__symbolLists:
            self.__symbolLists = []

        with open(os.path.join(self.__config.getDir(), symbolFile), "r") as f:
            for symbols in f:
                if symbols not in self.__symbolLists:
                    self.__symbolLists.append([symbol.strip() for symbol in symbols.split()])

        assert self.__symbolLists, "None symbol provided"

    def runTests(self):
        ''' run tests '''
        for symbols in self.__symbolLists:
            try:
                self._runOneTest(symbols)
            except KeyboardInterrupt:
                LOG.error("User Interrupted")
                sys.exit("User Interrupted")
            except BaseException as excp:
                LOG.error("Unexpected error when backtesting %s -- except %s, traceback %s"
                          % (symbols, excp, traceback.format_exc(8)))

    def getMetrics(self):
        ''' get all metrics '''
        return self.__mCalculator.getMetrics()

    def printMetrics(self):
        ''' print metrics '''
        LOG.info(self.getMetrics())


class TestRunner(object):
    ''' back testing '''
    def __init__(self, config, metricManager, accounts, symbols, startTickDate, endTradeDate, cash, trade_type):
        self.trade_type = trade_type
        self.__accountManager = AccountManager()
        self.__accountId = None
        self.__startTickDate = startTickDate
        self.__endTradeDate = endTradeDate
        self.__tickFeeder = TickFeeder(start=startTickDate, end=endTradeDate, trade_type=trade_type)
        self.__tradingCenter = TradingCenter()
        self.__tradingEngine = TradingEngine()
        self.__indexHelper = IndexHelper()
        self.__accounts = accounts
        self.__history = History()
        self.__saver = None
        self.__symbols = symbols
        self.__config = config
        self.__metricManager = metricManager
        self.__cash = cash

    def _setup(self):
        ''' setup '''
        self._setupTradingCenter()
        self._setupTickFeeder()
        self._setupSaver()

        # wire things together
        self._setupStrategy()
        self.__tickFeeder.tradingCenter = self.__tradingCenter
        self.__tradingEngine.tickProxy = self.__tickFeeder
        self.__tradingEngine.orderProxy = self.__tradingCenter
        self.__tradingCenter.accountManager = self.__accountManager
        self.__tradingEngine.saver = self.__saver
        self.__tickFeeder.saver = self.__saver
        self.__accountManager.saver = self.__saver

    def _setupTradingCenter(self):
        self.__tradingCenter.start = 0
        self.__tradingCenter.end = None

    def _setupTickFeeder(self):
        ''' setup tickFeeder'''
        self.__tickFeeder.indexHelper = self.__indexHelper
        self.__tickFeeder.setSymbols(self.__symbols)
        self.__tickFeeder.setDam(self._createDam(""))  # no need to set symbol because it's batch operation

        iSymbol = self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_INDEX)
        self.__tickFeeder.setIndexSymbol(iSymbol)

    def _createDam(self, symbol):
        ''' setup Dam'''
        damName = self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_INPUT_DAM)
        inputDb = self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_INPUT_DB)
        dam = DAMFactory.createDAM(damName, {'db': inputDb})
        dam.setSymbol(symbol)

        return dam

    def _setupSaver(self):
        ''' setup Saver '''
        saverName = self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_SAVER)
        outputDbPrefix = self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_OUTPUT_DB_PREFIX)
        if saverName:
            self.__saver = StateSaverFactory.createStateSaver(saverName,
                                                              {'db': outputDbPrefix + getBackTestResultDbName(self.__symbols,
                                                                                                              self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_STRATEGY_NAME),
                                                                                                              self.__startTickDate,
                                                                                                              self.__endTradeDate)})

    def _setupStrategy(self):
        ''' setup tradingEngine'''
        strategy = StrategyFactory.createStrategy(
                self.config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_STRATEGY_NAME),
                self.config.getSection(CONF_ULTRAFINANCE_SECTION),
                self.symbols,
                self.history,
                self.account,
                self.trading_engine)

        # register on trading engine
        strategy.tradingEngine = self.__tradingEngine
        self.__tradingEngine.register(strategy)

    def _execute(self):
        ''' run backtest '''
        LOG.info("Running backtest for %s" % self.__symbols)
        # start trading engine
        thread = Thread(target=self.__tradingEngine.runListener, args=())
        thread.setDaemon(False)
        thread.start()

        # start tickFeeder
        self.__tickFeeder.execute()
        self.__tickFeeder.complete()

        timePositions = self.__accountManager.getAccountPostions(self.__accountId)
        startTradeDate = self.__config.getOption(CONF_ULTRAFINANCE_SECTION, CONF_START_TRADE_DATE)
        if startTradeDate:
            startTradeDate = int(startTradeDate)
            timePositions = [tp for tp in timePositions if tp[0] >= startTradeDate]

        # get and save metrics
        result = self.__metricManager.calculate(self.__symbols, timePositions, self.__tickFeeder.iTimePositionDict)
        account = self.__accountManager.getAccount(self.__accountId)
        self.__saver.writeMetrics(result[BasicMetric.START_TIME],
                                  result[BasicMetric.END_TIME],
                                  result[BasicMetric.MIN_TIME_VALUE][1],
                                  result[BasicMetric.MAX_TIME_VALUE][1],
                                  result[BasicMetric.SRATIO],
                                  result[BasicMetric.MAX_DRAW_DOWN][1],
                                  result[BasicMetric.R_SQUARED],
                                  account.getTotalValue(),
                                  account.holdings)

        # write to saver
        LOG.debug("Writing state to saver")
        self.__saver.commit()

        self.__tradingEngine.stop()
        thread.join(timeout=240)

    def _printResult(self):
        ''' print result'''
        account = self.__accountManager.getAccount(self.__accountId)
        self.__accounts.append(account)
        LOG.info("account %s" % account)
        LOG.debug([str(order) for order in account.orderHistory])
        LOG.debug("account position %s" % self.__accountManager.getAccountPostions(self.__accountId))

    def runTest(self):
        ''' run one test '''
        self._setup()
        self._execute()
        self._printResult()


# ###########Util function################################
def getBackTestResultDbName(symbols, strategyName, startTickDate, endTradeDate):
    ''' get table name for back test result'''
    return "%s__%s__%s__%s" % ('_'.join(symbols) if len(symbols) <= 1 else len(symbols), strategyName, startTickDate, endTradeDate if endTradeDate else "Now")

if __name__ == "__main__":
    backtester = BackTester("backtest_zscoreMomentumPortfolio.ini", startTickDate=19901010, startTradeDate=19901010, endTradeDate=20131010)
    backtester.setup()
    backtester.runTests()
    backtester.printMetrics()
