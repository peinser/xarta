FROM sqitch/sqitch:v1.4.1.1 AS sqitch-base

WORKDIR /app
COPY --chown=sqitch:sqitch ./docker/db/entrypoint.sh ./entrypoint.sh
COPY --chown=sqitch:sqitch ./db/ .

ENTRYPOINT sh entrypoint.sh $0 $@
