import os
import warnings
from datetime import date

import helpers_gvb_reworked_v2 as h
import pandas as pd
import requests
from workalendar.europe import Netherlands

stations = ['Centraal Station', 'Station Zuid']


def read_data():
    today = pd.to_datetime("today")
    today_str = str(today.year) + "-" + str(today.month) + "-" + str(today.day)
    covid_url = 'https://covidtrackerapi.bsg.ox.ac.uk/api/v2/stringency/date-range/2020-09-01/' + today_str

    print('Start loading raw data')

    herkomst_2020 = h.get_gvb_data('Datalab_Reis_Herkomst_Uur_2020')
    bestemming_2020 = h.get_gvb_data('Datalab_Reis_Bestemming_Uur_2020')

    herkomst_2021 = h.get_gvb_data('Datalab_Reis_Herkomst_Uur_2021')
    bestemming_2021 = h.get_gvb_data('Datalab_Reis_Bestemming_Uur_2021')

    bestemming_2021 = h.get_gvb_data_json(bestemming_2021)

    knmi_obs = h.get_knmi_data('knmi/knmi-observations/2021/**/**/*')
    knmi_preds = h.get_knmi_data('knmi/knmi/**/**/**/*.json.gz')

    covid_measures = h.get_covid_measures()

    covid_df_raw = pd.DataFrame(requests.get(url=covid_url).json()['data'])
    holidays_data_raw = Netherlands().holidays(2019) + Netherlands().holidays(2020) + Netherlands().holidays(2021)
    vacations_df = h.get_vacations()
    events = h.get_events()

    return herkomst_2020, bestemming_2020, herkomst_2021, bestemming_2021, knmi_obs, knmi_preds, covid_measures, covid_df_raw, holidays_data_raw, vacations_df, events


def preprocess_data(herkomst_2020, bestemming_2020, herkomst_2021, bestemming_2021, knmi_obs, knmi_preds,
                    covid_measures, covid_df_raw, holidays_data_raw, vacations_df, events):
    print('Start pre-processing data')

    herkomst = pd.concat([herkomst_2020, herkomst_2021])
    bestemming = pd.concat([bestemming_2020, bestemming_2021])

    # Cast 'AantalReizen' to int to sum up
    bestemming['AantalReizen'] = bestemming['AantalReizen'].astype(int)
    herkomst['AantalReizen'] = herkomst['AantalReizen'].astype(int)

    # Remove all duplicates
    bestemming.drop_duplicates(inplace=True)
    herkomst.drop_duplicates(inplace=True)

    # Group by station name because we are analysing per station
    bestemming_grouped = \
        bestemming.groupby(['Datum', 'UurgroepOmschrijving (van aankomst)', 'AankomstHalteNaam'], as_index=False)[
            'AantalReizen'].sum()
    herkomst_grouped = \
        herkomst.groupby(['Datum', 'UurgroepOmschrijving (van vertrek)', 'VertrekHalteNaam'], as_index=False)[
            'AantalReizen'].sum()

    bestemming_herkomst = h.merge_bestemming_herkomst(bestemming_grouped, herkomst_grouped)

    gvb_dfs = []

    for station in stations:
        gvb_dfs.append(h.preprocess_gvb_data_for_modelling(bestemming_herkomst, station))

    knmi_historical = h.preprocess_knmi_data_hour(knmi_obs)
    knmi_forecast = h.preprocess_metpre_data(knmi_preds)
    covid_df = h.preprocess_covid_data(covid_df_raw)
    holiday_df = h.preprocess_holiday_data(holidays_data_raw)

    gvb_dfs_merged = []
    for df in gvb_dfs:
        gvb_dfs_merged.append(
            h.merge_gvb_with_datasources(df, knmi_historical, covid_df, covid_measures, holiday_df, vacations_df,
                                         events))

    return gvb_dfs_merged, covid_df, holiday_df, knmi_forecast


def clean_data(gvb_dfs_merged):
    print('Start cleaning data')

    # Interpolate missing data
    gvb_dfs_interpolated = []
    for df in gvb_dfs_merged:
        gvb_dfs_interpolated.append(h.interpolate_missing_values(df))

    gvb_dfs_final = []
    for df in gvb_dfs_interpolated:
        df['check-ins'] = df['check-ins'].astype(int)
        df['check-outs'] = df['check-outs'].astype(int)
        df[['check-ins_week_ago', 'check-outs_week_ago']] = df.apply(lambda x: h.get_crowd_last_week(df, x), axis=1,
                                                                     result_type="expand")

        gvb_dfs_final.append(df)

    return gvb_dfs_final


def split_data_for_modelling(gvb_dfs_final, covid_df, covid_measures, holiday_df, vacations_df, knmi_forecast, events,
                             features):
    targets = ['check-ins', 'check-outs']

    print('Split data for modelling')
    data_splits = []
    for df in gvb_dfs_final:
        df = df[['datetime'] + features + targets]

        train, validation, test = h.get_train_val_test_split(df.dropna())
        data_splits.append([train, validation, test])

    X_train_splits = []
    y_train_splits = []

    X_validation_splits = []
    y_validation_splits = []

    X_test_splits = []
    y_test_splits = []

    for split in data_splits:
        X_train_splits.append(split[0][features])
        y_train_splits.append(split[0][targets])

        X_validation_splits.append(split[1][features])
        y_validation_splits.append(split[1][targets])

        X_test_splits.append(split[2][features])
        y_test_splits.append(split[2][targets])

    # Dataframes to predict cdfheck-ins and check-outs of next week
    X_predict_dfs = []

    for df in gvb_dfs_final:
        X_predict_dfs.append(
            h.get_future_df(features, df, covid_df.tail(1)['stringency'][0], covid_measures, holiday_df, vacations_df,
                            knmi_forecast, events))

    return data_splits, X_train_splits, y_train_splits, X_validation_splits, y_validation_splits, X_test_splits, y_test_splits, X_predict_dfs


def model(data_splits, X_train_splits, y_train_splits, X_validation_splits, y_validation_splits, X_test_splits,
          y_test_splits):
    print('Start modelling')

    # basic_models = []
    #
    # for x in range(0, len(data_splits)):
    #     model_basic, r_squared_basic, mae_basic, rmse_basic = h.train_random_forest_regressor(X_train_splits[x],
    #                                                                                           y_train_splits[x],
    #                                                                                           X_validation_splits[x],
    #                                                                                           y_validation_splits[x],
    #                                                                                           None)
    #     basic_models.append([model_basic, r_squared_basic, mae_basic, rmse_basic])

    # Specify hyperparameters, these could be station-specific. For now, default hyperparameter settings are being used.
    centraal_station_hyperparameters = None
    station_zuid_hyperparameters = None
    # station_bijlmer_arena_hyperparameters = 2

    hyperparameters = [centraal_station_hyperparameters,
                       station_zuid_hyperparameters
                       #                  ,station_bijlmer_arena_hyperparameters
                       ]

    test_models = []

    for x in range(0, len(data_splits)):
        X_train_with_val = pd.concat([X_train_splits[x], X_validation_splits[x]])
        y_train_with_val = pd.concat([y_train_splits[x], y_validation_splits[x]])

        model_test, r_squared_test, mae_test, rmse_test = h.train_random_forest_regressor(X_train_with_val,
                                                                                          y_train_with_val,
                                                                                          X_test_splits[x],
                                                                                          y_test_splits[x],
                                                                                          hyperparameters[x])
        test_models.append([model_test, r_squared_test, mae_test, rmse_test])

    h.log_models(test_models, stations)

    for x in range(0, len(test_models)):
        station_name = stations[x]
        r_squared = test_models[x][1]
        if r_squared < 0.7:
            warnings.warn("Model for " + station_name + " shows unexpected performance!")

    final_models = []

    for x in range(0, len(data_splits)):
        X_train_with_val = pd.concat([X_train_splits[x], X_validation_splits[x], X_test_splits[x]])
        y_train_with_val = pd.concat([y_train_splits[x], y_validation_splits[x], y_test_splits[x]])

        model_final = \
            h.train_random_forest_regressor(X_train_with_val, y_train_with_val, X_test_splits[x], y_test_splits[x],
                                            hyperparameters[x])[0]
        final_models.append(model_final)

    return final_models


def predict_and_save(models, X_predict_dfs):
    print('Start preparing data')

    predictions = []
    for i, model in enumerate(models):
        prediction = h.predict(model, X_predict_dfs[i].dropna())
        predictions.append(prediction)

    for i, prediction in enumerate(predictions):
        if not os.path.exists('output/' + stations[i]):
            os.mkdir('output/' + stations[i])

        prediction.to_csv(
            ('output/{}/prediction_next_week_{}.csv'.format(stations[i], date.today().strftime("%b-%d-%Y"))))