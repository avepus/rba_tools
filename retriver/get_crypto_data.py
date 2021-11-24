# -*- coding: utf-8 -*-
"""Retrives and stores data in a local sqlite database

Created on Sat Apr 25 22:09:42 2020

@author: Avery

"""
from abc import ABC, abstractmethod
from typing import Type
import ccxt
import pandas as pd
import numpy as np
from time import sleep
from datetime import datetime, date, timedelta
from dateutil import parser,tz
import rba_tools.retriver.database as database
from rba_tools.utils import convert_timeframe_to_ms,get_table_name_from_str,get_table_name_from_dataframe
import sqlite3
from pathlib import Path
from dataclasses import dataclass

DATAFRAME_HEADERS = ['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume', 'Symbol', 'Is_Final_Row']
INDEX_HEADER = 'Timestamp'

def get_empty_ohlcv_df():
    return pd.DataFrame(columns=DATAFRAME_HEADERS).set_index(INDEX_HEADER)


class OHLCVDatabaseInterface(ABC): #need to split into two classes
    @abstractmethod
    def store_dataframe(self, df: pd.DataFrame, timeframe: str) -> None:
        """stores pandas dataframe data into database"""

    @abstractmethod
    def get_query_result_as_dataframe(self, query: str, timeframe: str) -> pd.DataFrame:
        """executes an SQL query and returns results in dataframe"""

class SQLite3OHLCVDatabase(OHLCVDatabaseInterface):

    def __init__(self, test=False):
        db_file = 'ohlcv_sqlite_test.db' if test else 'ohlcv_sqlite.db'
        self.database_file = str(Path(__file__).parent) + '\\ohlcv_data\\' + db_file
        self.connection = None
        
    def store_dataframe(self, df: pd.DataFrame, timeframe: str):
        connection = sqlite3.connect(self.get_database_file())
        table_name = get_table_name_from_dataframe(df)
        try:
            df.to_sql(table_name, connection)
        finally:
            connection.close()

    def get_query_result_as_dataframe(self, query: str):
        connection = sqlite3.connect(self.get_database_file())
        result = get_empty_ohlcv_df()
        try:
            result = pd.read_sql_query(query, connection)
        finally:
            connection.close()
        return result

    def create_table_if_not_exists(self, table_name: str) -> None:
        sql_create_ohlcv_table = f""" CREATE TABLE IF NOT EXISTS {table_name} (
                                        Symbol string NOT NULL,
                                        Timestamp integer NOT NULL,
                                        Open real NOT NULL,
                                        High real NOT NULL,
                                        Low real NOT NULL,
                                        Close real NOT NULL,
                                        Volume integer NOT NULL,
                                        Is_Final_Row integer,
                                        PRIMARY KEY (Symbol, Timestamp)
                                        CHECK(Is_Final_Row == 0 or Is_Final_Row == 1 or Is_Final_Row is NULL)
                                    ); """
        connection = sqlite3.connect(self.get_database_file())
        try:
            cursor = connection.cursor()
            cursor.execute(sql_create_ohlcv_table)
        finally:
            connection.close()

    def _execute_query(self, query: str):
        """execute and return data from a query. Meant only for troubleshooting"""
        connection = sqlite3.connect(self.database_file)
        cursor = connection.cursor()
        try:
            cursor.execute(query)
            data = cursor.fetchall()
        finally:
            connection.close()
        return data

    def get_database_file(self):
        return self.database_file

class OHLCVDataRetriver(ABC):
    "pulls OHLCV data for a specific symbol, timeframe, and date range"

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str, from_date: date, to_date: date) -> pd.DataFrame:
        """obtains OHLCV data"""

    def get_from_and_to_datetimes(self, from_date: date, to_date: date):
        from_datetime = datetime.combine(from_date, datetime.min.time())
        #add one day minus 1 second to get all the data from the end_date. Need for timeframes < 1 day
        to_datetime = datetime.combine(to_date, datetime.min.time()) + timedelta(seconds = -1, days=1)
        return (from_datetime, to_datetime)

class CCXTDataRetriver(OHLCVDataRetriver):

    def __init__(self, exchange: str):
        exchange_class = getattr(ccxt, exchange)
        self.exchange = exchange_class({
                            'timeout': 30000,
                            'enableRateLimit': True,
                            })
    
    def fetch_ohlcv(self, symbol: str, timeframe: str, from_date: date, to_date: date) -> pd.DataFrame:
        from_datetime, to_datetime = self.get_from_and_to_datetimes(from_date, to_date)
        from_date_ms = convert_datetime_to_UTC_Ms(from_datetime)
        to_date_ms = convert_datetime_to_UTC_Ms(to_datetime)
        data = self.get_all_ccxt_data(symbol, timeframe, from_date_ms, to_date_ms)
        return self.format_ccxt_returned_data(data, symbol, to_datetime)

    def format_ccxt_returned_data(self, data, symbol, to_date) -> pd.DataFrame:
        """formats the data pulled from ccxt into the expected format"""
        if not data:
            return get_empty_ohlcv_df()
        header = [INDEX_HEADER, 'Open', 'High', 'Low', 'Close', 'Volume']
        df = pd.DataFrame(data, columns=header).set_index(INDEX_HEADER)
        df.index = pd.to_datetime(df.index, unit='ms')
        df['Symbol'] = symbol
        df['Is_Final_Row'] = np.nan
        return df.loc[:to_date].copy()

    def get_all_ccxt_data(self, symbol, timeframe, from_date_ms, to_date_ms):
        """pull ccxt data repeatedly until we have all data"""
        call_count = 1
        return_data = None
        to_date_is_found_or_passed = False
        while not to_date_is_found_or_passed:
            print(f'Fetching {symbol} market data from {self.exchange}. call #{call_count}')
            data = self.exchange.fetch_ohlcv(symbol, timeframe, since=from_date_ms)
            sleep(self.exchange.rateLimit / 1000)
            if not data: #handle when we don't get any data by returning what we have so far
                return return_data
            call_count += 1
            if return_data:
                return_data.extend(data)
            else:
                return_data = data
            to_date_is_found_or_passed = any(to_date_ms == row[0] or to_date_ms < row[0] for row in data)
            last_end_timestamp_ms = data[len(data) - 1][0]
            from_date_ms = last_end_timestamp_ms + 1 #add one to not grab same time twice
        return return_data

class CSVDataRetriver(OHLCVDataRetriver):

    def __init__(self, file):
        self.file = file
        
    def fetch_ohlcv(self, symbol: str, timeframe: str, from_date: datetime, to_date: datetime) -> pd.DataFrame:
        from_datetime, to_datetime = self.get_from_and_to_datetimes(from_date, to_date)
        data = pd.read_csv(self.file, index_col=INDEX_HEADER, parse_dates=True)
        return self.format_csv_data(data, symbol, from_datetime, to_datetime)

    def format_csv_data(self, data, symbol: str, from_date: datetime, to_date: datetime):
        df = data.loc[data['Symbol'] == symbol]
        return df.loc[from_date:to_date].copy()

class DatabaseRetriver(OHLCVDataRetriver):
    """pulls data from a OHLCVDatabase database"""
    
    def __init__(self, database: Type[OHLCVDatabaseInterface]):
        self.database = database

    def fetch_ohlcv(self, symbol: str, timeframe: str, from_date: datetime, to_date: datetime) -> pd.DataFrame:
        query = self.get_query(symbol, timeframe, from_date, to_date)
        query_result = self.database.get_query_result_as_dataframe(query)
        return self.format_database_data(query_result)

    def format_database_data(self, data):
        if not data:
            return get_empty_ohlcv_df()
        data[INDEX_HEADER] = pd.to_datetime(data[INDEX_HEADER])
        data['Is_Final_Row'] = pd.to_numeric(data['Is_Final_Row'], errors='coerce')
        return data.set_index(INDEX_HEADER)

    def get_query(self, symbol: str, timeframe: str, from_date: datetime, to_date: datetime):
        """Generate query based on fetch_ohlcv parameters"""
        symbol_condition = "Symbol = '" + symbol + "'"
        table_name = get_table_name_from_str(timeframe)
        start_condition = f'and {INDEX_HEADER} >= "{from_date}"'
        end_condition = f'and {INDEX_HEADER} <= "{to_date}"'
        return f"""SELECT * FROM {table_name} 
            WHERE {symbol_condition}
            {start_condition}
            {end_condition}"""

def checked_retrieved_data(data: pd.DataFrame, symbol: str, timeframe: str, from_date_str: str, to_date_str: str=None):
    """checks if the data retrieved has all the data"""


def main(symbol: str, timeframe: str, from_date_str: str, to_date_str: str=None, stored_retriever: Type[OHLCVDataRetriver]=None, online_retriever: Type[OHLCVDataRetriver]=None, database: Type[OHLCVDatabaseInterface]=None):
    """retrieves a dataframe from saved database if possible otherwise from online"""
    from_date = parser.parse(from_date_str).date()
    if to_date_str:
        to_date = parser.parse(to_date_str).date()
    else:
        #default to_date to yesterday
        to_date = from_date.today() - timedelta(days=1)

    all_data = get_empty_ohlcv_df()
    online_data = get_empty_ohlcv_df()

    #retrieve data from stored database if we have one
    if stored_retriever:
        all_data = stored_retriever.fetch_ohlcv(symbol, timeframe, from_date, to_date)

    #pull extra data if we have one
    if online_retriever:
        prior_pull_end_date = needs_former_data(all_data, from_date)
        if prior_pull_end_date:
            online_data = online_retriever.fetch_ohlcv(symbol, timeframe, from_date, prior_pull_end_date)
            database.store_dataframe(online_data)
            all_data = all_data.append(online_data).sort_index()

        post_pull_from_date = needs_later_data(all_data, to_date)
        if post_pull_from_date:
            online_data = online_retriever.fetch_ohlcv(symbol, timeframe, post_pull_from_date, to_date)
            database.store_dataframe(online_data)
            all_data = all_data.append(online_data).sort_index()

    return all_data

def needs_former_data(data: pd.DataFrame, from_date: date):
    if not data:
        return from_date
    if from_date not in data.index:
        return min(data.index) - timedelta(days=1)
    return None


def needs_later_data(data: pd.DataFrame, to_date: date):
    if not data:
        return to_date
    if to_date not in data.index:
        return min(data.index) + timedelta(days=1)
    return None
    
def main_def(symbol: str, timeframe: str, from_date_str: str, to_date_str: str=None):
    database = SQLite3OHLCVDatabase()
    stored_retriver = DatabaseRetriver(database)
    online_retriver = CCXTDataRetriver('binance')

    return main(symbol, timeframe, from_date_str, to_date_str, stored_retriver, online_retriver, database)



    

#old code below
###########################################################


def getBinanceExchange():
    """Gets ccxt class for binance exchange"""
    exchange_id = 'binance'
    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class({
        'timeout': 30000,
        'enableRateLimit': True,
        })

def fetch_ohlcv_dataframe_from_exchange(symbol, exchange=None, timeFrame = '1d', start_time_ms=None, last_request_time_ms=None):
    """
    testAttempts to retrieve data from an exchange for a specified symbol
    
    Returns data retrieved from ccxt exchange in a pandas dataframe
    with Symbol and Is_Final_Row columns
    Parameters:
        symbol (str) -- symbol to gather market data on (e.g. "BCH/BTC")
        exchange (ccxt class) -- ccxt exchange to retrieve data from. Default is binance
        timeFrame (str) -- timeframe for which to retrieve the data
        start_time_ms (int) -- UTC timestamp in milliseconds
        last_request_time_ms (timestamp ms) -- timestamp of last call used to throttle number of calls
    
    Returns:
        DataFrame: retrived data in Pandas DataFrame with ms timestamp index
    """
    if type(start_time_ms) == str:
        start_time_ms = convert_datetime_to_UTC_Ms(get_UTC_datetime(start_time_ms))
    if exchange is None:
        exchange = getBinanceExchange()
    header = [INDEX_HEADER, 'Open', 'High', 'Low', 'Close', 'Volume']
    if not exchange.has['fetchOHLCV']:
        print(exchange, "doesn't support fetchOHLCV")
        return pd.DataFrame([], columns=header) #empty dataframe
    now = convert_datetime_to_UTC_Ms()
    if last_request_time_ms is not None and (now - last_request_time_ms) < 1000:
        sleep(1000 - (now - last_request_time_ms))
    last_request_time_ms = convert_datetime_to_UTC_Ms()
    print('lastCall =',last_request_time_ms,'fetching data...')
    data = exchange.fetch_ohlcv(symbol, timeFrame, since=start_time_ms)
    df = pd.DataFrame(data, columns=header).set_index('Timestamp')
    df['Symbol'] = symbol
    df['Is_Final_Row'] = np.nan
    return df


def getAllTickers(exchange):
    if not exchange.has['fetchTickers']:
        print("Exchange cannot fetch all tickers")
    else:
        return exchange.fetch_tickers()


def getAllSymbolsForQuoteCurrency(quoteSymbol, exchange):
    """
    Get's list of all currencies with the input quote currency
    
    Returns a list
    Parameters:
        quoteSymbol (str) -- symbol to check base curriencies for (e.g. "BTC")
        exchange (ccxt class) -- ccxt excahnge to retrieve data from
    """
    if "/" not in quoteSymbol:
        quoteSymbol = "/" + quoteSymbol
    ret = []
    allTickers = exchange.fetch_tickers()
    for symbol in allTickers:
        if quoteSymbol in symbol:
            ret.append(symbol)
    return ret


def get_DataFrame(symbol_list, exchange=None, from_date_str='1/1/1970', end_date_str='1/1/2050', ret_as_list=False, timeframe = '1d', max_calls=10):
    """gets a dataframe in the expected format
    
    Parameters:
        symbol_list (str/list) -- symbol(s) to get market data (e.g. "BCH/BTC")
        exchange (ccxt class) -- ccxt exchange to retrieve data from
        from_date_str (str) -- string representation of start date timeframe
        end_date_str (str) -- string representation of end date timeframe
        ret_as_list (bool) -- boolean indicating to return list of dfs or single df
        timeFrame (str) -- timeframe to pull in string format like '3h'
        maxCalls (int) -- max number of data pulls for a given currency
                            intended for use as safety net to prevent too many calls

    Returns:
        DataFrame: retrived data in Pandas DataFrame with ms timestamp index
    """
    if ret_as_list:
        return_df = []
    else:
        return_df = pd.DataFrame()
    with database.OHLCVDatabase() as connection:
        df = get_saved_data(symbol_list, connection, from_date_str, end_date_str, timeframe=timeframe) 

    from_date = parser.parse(from_date_str)
    end_date = parser.parse(end_date_str)

    from_date_ms = convert_datetime_to_UTC_Ms(from_date)
    end_date_ms = convert_datetime_to_UTC_Ms(end_date)

    for symbol in symbol_list:
        symbol_df = df.loc[df['Symbol'] == symbol] #grab just this symbol data since they're all in one dataframe
        if symbol_df.empty and exchange is not None:
            symbol_df = retrieve_data_from_exchange(symbol, exchange, from_date_ms, end_date_ms, timeframe, max_calls)
            if symbol_df.empty:
                print('Failed to retrieve',symbol,'data')
                continue
            symbol_df.to_sql('OHLCV_DATA', connection, if_exists='append')
            symbol_df.index = pd.to_datetime(symbol_df.index, unit='ms')

        elif exchange is not None:
            first_df_timestamp = symbol_df.index.min().item() #.item() gets us the native python number type instead of the numpy type
            need_prior_data = (first_df_timestamp != from_date_ms) and (symbol_df.at[first_df_timestamp,'Is_Final_Row'] != 1)
            if need_prior_data:
                first_df_timestamp -= 1 #we subtract 1 to the timestamp to pull the up to this timestamp which avoids duplicates
                first_df_timestamp_str = datetime.fromtimestamp(first_df_timestamp / 1000, tz.tzutc())
                print(f'Need earlier data for {symbol}. Retreiving data from {from_date_str} ({from_date_ms}) to {first_df_timestamp_str} ({first_df_timestamp})')
                prior_data = retrieve_data_from_exchange(symbol, exchange, from_date_ms, first_df_timestamp, timeframe, max_calls)
                prior_data.to_sql('OHLCV_DATA', connection, if_exists='append')
                symbol_df = symbol_df.append(prior_data).sort_index() #we sort index when appending prior data so it doesn't append to the end

            last_df_timestamp = symbol_df.index.max().item()
            two_days_ms = 2 * 24 * 60 * 60 * 1000
            last_timestamp_is_older_than_two_days = last_df_timestamp < (convert_datetime_to_UTC_Ms() - two_days_ms)
            need_later_data = last_df_timestamp != end_date_ms and symbol_df.at[last_df_timestamp,'Is_Final_Row'] != 1 and last_timestamp_is_older_than_two_days
            if need_later_data:
                last_df_timestamp += 1 #we add 1 to the timestamp to pull the next timestamp data and to avoid duplicates
                last_df_timestamp_str = datetime.fromtimestamp(last_df_timestamp / 1000, tz.tzutc())
                print(f'Need later data for {symbol}. Retreiving data from {last_df_timestamp_str} ({last_df_timestamp}) to {end_date_str} ({end_date_ms})')
                later_data = retrieve_data_from_exchange(symbol, exchange, last_df_timestamp, end_date_ms, timeframe, max_calls)
                later_data.to_sql('OHLCV_DATA', connection, if_exists='append')
                symbol_df = symbol_df.append(later_data)

        else:
            raise NameError('Data is missing for',symbol,'but no exchange was passed in to retrieve data from')
        if ret_as_list:
            return_df.append(set_data_timestamp_index(symbol_df))
        else:
            return_df = return_df.append(set_data_timestamp_index(symbol_df))
    connection.close()
    return return_df


def populate_is_final_column(df, from_date_ms, end_date_ms, timeframe):
    """populates Is_Final_Column to indicate that no past or future data
    exists past that row
    
    Parameters:
        df (DataFrame) -- dataframe for which to popualte the column
        from_date_ms (int) -- from date in milliseconds
        end_date_ms (int) -- end date in milliseconds
        timeframe (str) -- timeframe that data is in (e.g. 1h, 1d, 1m, etc.)
    
    Returns:
        DataFrame: updated DataFrame (note the input DataFrame is modified)
    """
    #if our from date is less than one bar behind our earliest data then there is no previous data
    timeframe_ms = convert_timeframe_to_ms(timeframe)
    if from_date_ms < (df.index.min().item() - timeframe_ms): 
        df.at[df.index.min(),'Is_Final_Row'] = 1
    two_timeframes_ms = 2 * timeframe_ms
    end_date_is_older_than_two_timeframes = end_date_ms < (convert_datetime_to_UTC_Ms() - two_timeframes_ms)
    #if our end date is greater than one bar past our latest data then this is no future data
    if end_date_ms > (df.index.max() + timeframe_ms) and end_date_is_older_than_two_timeframes:
        df.at[df.index.max(),'Is_Final_Row'] = 1
    return df
    

def retrieve_data_from_exchange(symbol, exchange, from_date_ms, end_date_ms=None, timeframe = '1d', max_calls=10):
    """Retrives data from ccxt exchange
    
    pulls data from ccxt exchange in 500 bar increments until we have all the data
    betwen from_date_ms and end_date_ms or we hit max_calls. Also calls
    populate_is_final_column to populate the Is_Final_Column of the DataFrame
    Parameters:
        symbol_list (str/list) -- symbol(s) to get market data (e.g. "BCH/BTC")
        exchange (ccxt class) -- ccxt exchange to retrieve data from
        from_date_ms (int) -- from date in utc milliseconds
        end_date_ms (int) -- end date in utc milliseconds
        timeFrame (str) -- timeframe to pull (e.g. '1d' for day)
        maxCalls (int) -- max number of data pulls for a given currency
                            intended for use as safety net to prevent too many calls
    
    Returns:
        DataFrame: retrived data in Pandas DataFrame with ms timestamp index
    """
    if not end_date_ms:
        convert_datetime_to_UTC_Ms()
    sleep(exchange.rateLimit / 1000)
    call_count = 1
    print(f'Fetching {symbol} market data from {exchange}. call #{call_count}')
    df = fetch_ohlcv_dataframe_from_exchange(symbol, exchange, timeframe, from_date_ms)
    if df.empty:
        print('Failed to retrieve',symbol,'data')
        return pd.DataFrame()
    retdf = df
    while (len(df) == 500 and call_count < max_calls and retdf.index.max() < end_date_ms):
        call_count += 1
        new_from_date = df.index[-1].item() + 1 #add 1 to prevent retrival of the same date
        sleep(exchange.rateLimit / 1000)
        print(f'Fetching {symbol} market data from {exchange}. call #{call_count}')
        df = fetch_ohlcv_dataframe_from_exchange(symbol, exchange, timeframe, new_from_date)
        retdf = retdf.append(df)
    if len(df) == 500 and call_count >= max_calls and retdf.index.max() < end_date_ms:
        print(f'Maximum data retrivals ({max_calls}) hit.')
        return pd.DataFrame()
    populate_is_final_column(retdf, from_date_ms, end_date_ms, timeframe)
    return retdf.loc[from_date_ms:end_date_ms,:]
    

def get_saved_data(symbol_list, connection, from_date_str=None, end_date_str=None, timeframe=None):
    """Attempts to retrive data from saved database
    
    Parameters:
        symbol_list (str/list) -- symbol(s) to get market data (e.g. "BCH/BTC")
        connection (obj) -- connectiont to sql database
        from_date_str (str) -- string representation of start date timeframe
        end_date_str (str) -- string representation of end date timeframe
        timeFrame (str) -- timeframe to pull in string format like '3h'

    Returns:
        DataFrame: retrived data in Pandas DataFrame with ms timestamp index
    """
    comma_symbols = "','".join(symbol_list)
    symbol_condition = "Symbol in ('" + comma_symbols + "')"
    start_condition = ''
    end_condition = ''
    timeframe_condition = ''
    if from_date_str:
        start_date = convert_datetime_to_UTC_Ms(get_UTC_datetime(from_date_str))
        start_condition = f'and TIMESTAMP >= {start_date}'
    if end_date_str:
        end_date = convert_datetime_to_UTC_Ms(get_UTC_datetime(end_date_str))
        end_condition = f'and TIMESTAMP <= {end_date}'
    if timeframe:
        timeframe_ms = convert_timeframe_to_ms(timeframe)
        timeframe_condition = f'and TIMESTAMP % {timeframe_ms} = 0'
    query = f"""SELECT * FROM OHLCV_DATA 
        WHERE {symbol_condition}
        {start_condition}
        {end_condition}
        {timeframe_condition}"""
    try:
        df = pd.read_sql_query(query, connection)
    except Exception as e:
        print('Query failed: \n' + query)
        print(e)
        return get_empty_ohlcv_df()
    df['Is_Final_Row'] = pd.to_numeric(df['Is_Final_Row'], errors='coerce')
    df.set_index('Timestamp', inplace=True)
    return trim_data_outside_timeframe(df, timeframe) 

def trim_data_outside_timeframe(df, timeframe):
    """Removes rows from dataframe that are not in the timeframe
    
    Parameters:
        df (str/list) -- symbol(s) to get market data (e.g. "BCH/BTC")
        timeframe (str) -- timeframe to pull in string format like '3h'

    Returns:
        DataFrame: with non-timeframe rows removed
    """
    timeframe_ms = convert_timeframe_to_ms(timeframe)
    #get series of rows that are in the timeframe based on difference between rows
    in_timeframe_bool_series = df.index.to_series().diff() == timeframe_ms
    #the first row gets missed by the above line so this corrects that
    if df.empty:
        return df
    try:
        first_true = min(in_timeframe_bool_series.loc[in_timeframe_bool_series].index)
    except ValueError:
        return get_empty_ohlcv_df()
    in_timeframe_bool_series[in_timeframe_bool_series.index < first_true] = True
    return df.loc[in_timeframe_bool_series].copy()

def set_data_timestamp_index(df, col='Timestamp', unit='ms'):
    """converts column with lable "Timestamp" of a DataFrame
    to a datetime and makes it the index

    Args:
        df (DataFrame): Pandas dataframe

    Returns:
        DataFrame: new Pandas DataFrame with updated index
    """
    retdf = df.copy()
    if retdf.empty:
        return retdf
    if retdf.index.name != 'Timestamp':
        retdf = retdf.set_index('Timestamp')
    retdf.index = pd.to_datetime(retdf.index, unit=unit)
    return retdf


def get_UTC_datetime(datetime_string=None):
    if datetime_string is None:
        return datetime.now()
    return parser.parse(datetime_string).replace(tzinfo = tz.tzutc())


def convert_datetime_to_UTC_Ms(input_datetime=None):
    if input_datetime is None:
        input_datetime = datetime.now()
    return int(round(input_datetime.replace(tzinfo = tz.tzutc()).timestamp() * 1000))
   

if __name__ == '__main__':
    symbol = 'ETH/BTC'
    timeframe = '1d'
    from_date = datetime(2019, 12, 1)
    to_date = datetime(2021, 12, 3)
    retriver = CCXTDataRetriver('binance')
    result = retriver.fetch_ohlcv(symbol, timeframe, from_date, to_date)
