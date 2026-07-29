import datetime
import time
from collections import deque
from django.db.backends.signals import connection_created
from django.dispatch import receiver
from django.db import connection
from django.conf import settings
from django.db.utils import OperationalError

# Thread-safe deque to keep recent query logs
DB_QUERY_LOGS = deque(maxlen=100)

def db_query_logging_wrapper(execute, sql, params, many, context):
    start_time = time.time()
    success = True
    error_message = None
    try:
        return execute(sql, params, many, context)
    except Exception as e:
        success = False
        error_message = str(e)
        raise
    finally:
        duration = (time.time() - start_time) * 1000  # in ms
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # Determine query type
        sql_upper = sql.strip().upper() if sql else ""
        if sql_upper.startswith("SELECT"):
            q_type = "READ"
        elif any(sql_upper.startswith(pfx) for pfx in ("INSERT", "UPDATE", "DELETE")):
            q_type = "WRITE"
        elif any(sql_upper.startswith(pfx) for pfx in ("BEGIN", "COMMIT", "ROLLBACK")):
            q_type = "TRANSACTION"
        else:
            q_type = "OTHER"
            
        # Format query for display
        try:
            formatted_sql = sql
            if params:
                formatted_sql = sql % params
        except Exception:
            formatted_sql = f"{sql} (Params: {params})"
            
        DB_QUERY_LOGS.append({
            'timestamp': timestamp,
            'type': q_type,
            'sql': formatted_sql,
            'duration': f"{duration:.2f}ms",
            'success': success,
            'error': error_message
        })

@receiver(connection_created)
def register_query_wrapper(sender, connection, **kwargs):
    if db_query_logging_wrapper not in connection.execute_wrappers:
        connection.execute_wrappers.append(db_query_logging_wrapper)

def get_db_status():
    db_config = settings.DATABASES.get('default', {})
    engine = db_config.get('ENGINE', 'unknown')
    name = db_config.get('NAME', 'unknown')
    host = db_config.get('HOST', 'localhost')
    port = db_config.get('PORT', '')
    user = db_config.get('USER', '')
    
    # Check if we are connected to the live database
    # Live database is configured if DB_ENGINE environment is django.db.backends.postgresql
    is_live = (engine == 'django.db.backends.postgresql')
    
    status = "Disconnected"
    error_msg = None
    latency = None
    
    start_time = time.time()
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        status = "Connected"
        latency = (time.time() - start_time) * 1000
    except OperationalError as e:
        status = "Disconnected"
        error_msg = str(e)
    except Exception as e:
        status = "Error"
        error_msg = str(e)
        
    return {
        'status': status,
        'engine': engine.split('.')[-1] if '.' in engine else engine,
        'name': name,
        'host': host,
        'port': port,
        'user': user,
        'is_live': is_live,
        'latency': f"{latency:.2f}ms" if latency is not None else None,
        'error': error_msg
    }
