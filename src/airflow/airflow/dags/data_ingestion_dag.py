import os
from datetime import datetime, timedelta
import pandas as pd
import great_expectations as gx
from dotenv import load_dotenv
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sensors.filesystem import FileSensor
from great_expectations.exceptions import DataContextError
import mlflow
import sys
import logging
sys.path.append(
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), '../../..')
    )
)
# Load environment variables
load_dotenv()
from house_prices import FEATURE_COLUMNS
from house_prices.preprocess import preprocess

MONITOR_DIR = os.getenv("MONITOR_DIR")


# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def extract_data(**kwargs):
    import glob

    directory_to_monitor = MONITOR_DIR
    logger.info(f"Monitoring directory: {directory_to_monitor}")

    # Getting list of csv files
    csv_files = [
        f for f in glob.glob(os.path.join(directory_to_monitor, '*.csv'))
        if '_checked' not in f
    ]

    if not csv_files:
        logger.info("All csv files are already scanned. No new files")
        return

    with mlflow.start_run():
        mlflow.log_param("num_files", len(csv_files))
        logger.info(f"Number of new CSV files: {len(csv_files)}")

        for file_path in csv_files:
            logger.info(f"Processing file: {file_path}")
            df_raw = pd.read_csv(file_path)

            df = preprocess(df_raw[FEATURE_COLUMNS], is_training=False)
            kwargs['ti'].xcom_push(
                key='extracted_rows',
                value=df.to_dict(orient='records')
            )
            logger.info(f"Data extracted and pushed to XCom for file: {file_path}")

            # Rename the processed file
            new_file_path = file_path.replace('.csv', '_checked.csv')
            os.rename(file_path, new_file_path)
            logger.info(f"File renamed to: {new_file_path}")

def validate_data(**kwargs):
    try:
        # Specify the path to the Great Expectations project directory
        ge_project_path = os.getenv('GE')
        logger.info("Initializing Great Expectations context")
        context = gx.data_context.DataContext(ge_project_path)

        # Create a BatchRequest to load the data
        batch_request = gx.core.batch.BatchRequest(
            datasource_name="my_datasource",
            data_connector_name="default_inferred_data_connector_name",
            data_asset_name="extracted_data",
        )

        # Retrieve the validator
        validator = context.get_validator(batch_request=batch_request, expectation_suite_name="extracted_data.csv.warning")
        logger.info("Batch loaded successfully.")
        logger.info(validator.head())  # Check if data is loaded

        # Load the checkpoint configuration
        checkpoint_name = "my_checkpoint"
        logger.info(f"Loading checkpoint: {checkpoint_name}")
        checkpoint = context.get_checkpoint(checkpoint_name)

        # Run the checkpoint
        logger.info("Running checkpoint")
        checkpoint_result = checkpoint.run()

        with mlflow.start_run():
            mlflow.log_metric("validation_success", checkpoint_result["success"])
            logger.info(f"Validation success: {checkpoint_result['success']}")

            if checkpoint_result["success"]:
                logger.info("Data quality check passed.")
            else:
                logger.warning("Data quality check failed.")
                for validation_result in checkpoint_result["run_results"].values():
                    if not validation_result["validation_result"]["success"]:
                        logger.warning(validation_result["validation_result"])

            context.build_data_docs()
            validation_result_identifier = (
                checkpoint_result.list_validation_result_identifiers()[0]
            )
            context.open_data_docs(
                resource_identifier=validation_result_identifier
            )
            logger.info("Data Docs built and opened")

    except DataContextError as e:
        logger.error(f"Error loading GE context: {e}")
    except Exception as e:
        logger.error(f"An error occurred: {e}")

def log_statistics_to_postgres(statistics: pd.DataFrame):
    logger.info("Logging statistics to PostgreSQL")
    target_hook = PostgresHook(postgres_conn_id='target_postgres_conn')
    target_conn = target_hook.get_conn()
    target_cursor = target_conn.cursor()

    target_cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.feature_statistics (
            feature_name TEXT NOT NULL,
            mean DOUBLE PRECISION,
            variance DOUBLE PRECISION,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    logger.info("Ensured feature_statistics table exists")

    for feature, row in statistics.iterrows():
        target_cursor.execute(
            """
            INSERT INTO feature_statistics (feature_name, mean, variance)
            VALUES (%s, %s, %s)
            """,
            (feature, row['mean'], row['variance'])
        )
        logger.info(f"Logged statistics for feature: {feature}")

    target_conn.commit()
    target_cursor.close()
    target_conn.close()
    logger.info("Statistics logging completed")

def calculate_and_log_statistics(**kwargs):
    logger.info("Calculating and logging statistics")
    rows = kwargs['ti'].xcom_pull(
        key='extracted_rows', task_ids='extract_data'
    )
    df = pd.DataFrame(rows)

    statistics = df.describe().loc[['mean', 'std']].transpose()
    statistics['variance'] = statistics['std'] ** 2

    log_statistics_to_postgres(statistics)
    logger.info("Statistics calculated and logged")

def load_data(**kwargs):
    logger.info("Loading data into PostgreSQL")
    rows = kwargs['ti'].xcom_pull(
        key='extracted_rows', task_ids='extract_data'
    )

    target_hook = PostgresHook(postgres_conn_id='target_postgres_conn')
    target_conn = target_hook.get_conn()
    target_cursor = target_conn.cursor()

    target_cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.target_predictions (
            Foundation_BrkTil TEXT,
            Foundation_CBlock TEXT,
            Foundation_PConc TEXT,
            Foundation_Slab TEXT,
            Foundation_Stone TEXT,
            Foundation_Wood TEXT,
            KitchenQual_Ex TEXT,
            KitchenQual_Fa TEXT,
            KitchenQual_Gd TEXT,
            KitchenQual_TA TEXT,
            TotRmsAbvGrd TEXT,
            WoodDeckSF TEXT,
            YrSold TEXT,
            "1stFlrSF" TEXT,
            "SalePrice" TEXT,
            "PredictionTimestamp" TEXT
        );
    """)
    logger.info("Ensured target_predictions table exists")

    with mlflow.start_run():
        mlflow.log_param("num_rows", len(rows))
        logger.info(f"Number of rows to load: {len(rows)}")

        for row in rows:
            try:
                target_cursor.execute("""
                    INSERT INTO target_predictions (
                        Foundation_BrkTil, Foundation_CBlock, Foundation_PConc,
                        Foundation_Slab, Foundation_Stone, Foundation_Wood,
                        KitchenQual_Ex, KitchenQual_Fa, KitchenQual_Gd,
                        KitchenQual_TA, TotRmsAbvGrd, WoodDeckSF, YrSold,
                        "1stFlrSF"
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    );
                """, (
                    row['Foundation_BrkTil'], row['Foundation_CBlock'],
                    row['Foundation_PConc'], row['Foundation_Slab'],
                    row['Foundation_Stone'], row['Foundation_Wood'],
                    row['KitchenQual_Ex'], row['KitchenQual_Fa'],
                    row['KitchenQual_Gd'], row['KitchenQual_TA'],
                    row['TotRmsAbvGrd'], row['WoodDeckSF'],
                    row['YrSold'], row['1stFlrSF']
                ))
                logger.info(f"Inserted row into target_predictions: {row}")
            except Exception as e:
                logger.error(f"Error inserting row {row}: {e}")

    target_conn.commit()
    target_cursor.close()
    target_conn.close()
    logger.info("Data loading completed")

with DAG(
    'data_ingestion_dag',
    default_args={
        'owner': 'airflow',
        'start_date': datetime(2023, 1, 1),
        'retries': 1,
        'retry_delay': timedelta(minutes=5),
    },
    schedule=None,  # Set to None to run manually
    catchup=False,
) as dag:

    sense_file_task = FileSensor(
        task_id='sense_file',
        filepath=MONITOR_DIR,
        fs_conn_id='fs_default',
        poke_interval=10,
        timeout=600,
    )

    extract_data_task = PythonOperator(
        task_id='extract_data',
        python_callable=extract_data,
    )

    validate_data_task = PythonOperator(
        task_id='validate_data',
        python_callable=validate_data,
    )

    calculate_statistics_task = PythonOperator(
        task_id='calculate_and_log_statistics',
        python_callable=calculate_and_log_statistics,
    )

    load_data_task = PythonOperator(
        task_id='load_data',
        python_callable=load_data,
    )

    sense_file_task >> extract_data_task >> validate_data_task
    validate_data_task >> calculate_statistics_task >> load_data_task