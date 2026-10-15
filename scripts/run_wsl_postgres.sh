#!/bin/sh
set -e
mkdir -p /dev/shm/pgdata /run/postgresql
chown -R postgres:postgres /dev/shm/pgdata /run/postgresql
if [ ! -f /dev/shm/pgdata/PG_VERSION ]; then
    su - postgres -s /bin/sh -c 'initdb -D /dev/shm/pgdata -E UTF8'
    echo "listen_addresses = '*'" >> /dev/shm/pgdata/postgresql.conf
    echo "port = 55433" >> /dev/shm/pgdata/postgresql.conf
    echo "unix_socket_directories = '/run/postgresql,/dev/shm'" >> /dev/shm/pgdata/postgresql.conf
    echo "host all all 0.0.0.0/0 trust" >> /dev/shm/pgdata/pg_hba.conf
    echo "host all all ::0/0 trust" >> /dev/shm/pgdata/pg_hba.conf
fi
exec su - postgres -s /bin/sh -c 'postgres -D /dev/shm/pgdata'
