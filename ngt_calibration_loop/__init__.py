"""Shared library for the NGT calibration loop's Airflow DAGs.

Contains the pure processing logic (OMS/EOS querying, job-script preparation,
batching decisions) that used to live inside the transitions-based FSMs in
NGTLoopStep2/3/4.py. No Airflow imports here -- these functions are plain
Python so they can be unit tested without a running Airflow instance; the
DAGs in airflow_automation/airflow_dags/ are thin wrappers that call into this
package from PythonOperator callables.
"""
