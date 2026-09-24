-- Runs once, when the postgres volume is first created: a separate database for MLflow's
-- tracking and registry tables, so they never mix with the framework's own schema.
CREATE DATABASE mlflow;
