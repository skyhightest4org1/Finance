#!/usr/bin/env python
"""tenant_migrator

Usage:
    tenant_migrator.py -h | --help
    tenant_migrator.py --config=<config> --destination_pop=<destination_pop> --instance_ids=<instance_ids> [--sleep_time=<sleep_time>]

Options:
    -h --help                               Help Screen.
    --config=<config>                       Config file needed to run the script
    --destination_pop=<destination_pop>     Destination PopId of the Tenant Instances that needs to be migrated
    --instance_ids=<instance_ids>           Comma separated list of instance_ids that needs to be migrated
    --sleep_time=<sleep_time>               Sleep time in seconds after disabling API access [default: 60]

"""

from contextlib import contextmanager

import docopt
import configparser
import pymysql.cursors
import redis
import datetime
import time
import threading

args = docopt.docopt(__doc__)

config = configparser.ConfigParser()
config.read(args['--config'])

db_config = config['DB']
redis_source_config = config['REDIS_SOURCE']
redis_dest_config = config['REDIS_DESTINATION']
redis_csp_q_dest_config = config['REDIS_CSP_Q_DESTINATION']
redis_csp_q_source_config = config['REDIS_CSP_Q_SOURCE']

instance_ids = args['--instance_ids']
destination_pop = args['--destination_pop']
sleep_time = float(args['--sleep_time'])

source_redis = redis.StrictRedis(
    host=redis_source_config['host'],
    port=redis_source_config['port'],
    ssl=redis_source_config.get('ssl', False),
    password=redis_source_config.get('password', None))

dest_redis = redis.StrictRedis(
    host=redis_dest_config['host'],
    port=redis_dest_config['port'],
    ssl=redis_dest_config.get('ssl', False),
    password=redis_dest_config.get('password', None))

csp_q_dest_redis = redis.StrictRedis(
    host=redis_csp_q_dest_config['host'],
    port=redis_csp_q_dest_config['port'],
    ssl=redis_csp_q_dest_config.get('ssl', False),
    password=redis_csp_q_dest_config.get('password', None))

csp_q_source_redis = redis.StrictRedis(
    host=redis_csp_q_source_config['host'],
    port=redis_csp_q_source_config['port'],
    ssl=redis_csp_q_source_config.get('ssl', False),
    password=redis_csp_q_source_config.get('password', None))

weight_keys = ('offlinedlp:weights', 'offlinedlp:burst_user:weights',
               'offlinedlp:email_event:weights',
               'offlinedlp:slack_task:weights', 'offlinedlp:slack_task:weights:dms',
               'offlinedlp:slack_task:weights:channels', 'offlinedlp:slack_task:weights:groups',
               'offlinedlp:slack_task:weights:files_list')

casb_webhook_csps = [28111, 12800, 12802, 17406, 17411, 2134, 21423, 2308, 7008, 2337]
casb_polling_csps = [10934, 12672, 16121, 18057, 2207, 2586, 3007, 3258, 3427, 35040, 3669, 4191, 4404, 24718, 7154]


@contextmanager
def open_db_connection():
    connection = pymysql.connect(host=db_config['host'],
                                 port=int(db_config['port']),
                                 user=db_config['user'],
                                 password=db_config['password'],
                                 database=db_config['name'],
                                 charset='utf8mb4',
                                 cursorclass=pymysql.cursors.DictCursor)
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        connection.commit()
        connection.close()


def execute_sql(sql_template, *args):
    with open_db_connection() as cursor:
        sql = sql_template % (args)
        cursor.execute(sql)
        return cursor.fetchall()


def get_instance_details():
    sql = 'select id instance_id, tenant_id, csp_id from shn.TenantInstance where id in (%s)'
    return execute_sql(sql, instance_ids)


def soft_disable_api_access():
    sql = 'UPDATE shn.DlpServiceConfiguration SET Enabled=0 WHERE Instance_Id in (%s)'
    execute_sql(sql, instance_ids)


def api_access_disable_soft(instance_id):
    sql = 'UPDATE shn.DlpServiceConfiguration SET Enabled=0 WHERE Instance_Id in (%s)'
    execute_sql(sql, instance_id)


def soft_enable_api_access():
    sql = 'UPDATE shn.DlpServiceConfiguration SET Enabled=1 WHERE Instance_Id in (%s)'
    execute_sql(sql, instance_ids)


def api_access_enable_soft(instance_id):
    sql = 'UPDATE shn.DlpServiceConfiguration SET Enabled=1 WHERE Instance_Id in (%s)'
    execute_sql(sql, instance_id)


def get_pop_name_to_id_map():
    sql = 'SELECT id, name FROM shn.DlpZeusGroup'
    result = execute_sql(sql)
    pop_name_to_id_map = {}
    for record in result:
        pop_name_to_id_map[record['name'].lower()] = record['id']
    return pop_name_to_id_map


def get_dlp_zeus_group_csp_mapping_next_id():
    sql = "select NEXT_ID  from shn.ID_TABLE where TABLE_NAME='DlpZeusGroupCspMapping'"
    return execute_sql(sql)[0]['NEXT_ID']


def update_dlp_zeus_group_csp_mapping_next_id(next_id):
    sql = "update shn.ID_TABLE set NEXT_ID=%s where TABLE_NAME='DlpZeusGroupCspMapping'"
    return execute_sql(sql, next_id)


def update_dlp_zeus_group_csp_mapping(tenant_id, csp_id, instance_id, pop_id):
    result = execute_sql('select * from shn.DlpZeusGroupCspMapping where tenant_id=%s and csp_id=%s and instance_id=%s',
                         tenant_id, csp_id, instance_id)
    if bool(result):
        execute_sql('update shn.DlpZeusGroupCspMapping set DlpZeusGroup_Id=%s '
                    'where tenant_id=%s and csp_id=%s and instance_id=%s', pop_id, tenant_id, csp_id, instance_id)
    else:
        next_id = get_dlp_zeus_group_csp_mapping_next_id()
        update_dlp_zeus_group_csp_mapping_next_id(next_id + 1)
        sql = 'insert into shn.DlpZeusGroupCspMapping values (%s, %s, %s, %s, %s, "%s")'
        ts = time.time()
        timestamp = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        execute_sql(sql, next_id, csp_id, tenant_id, instance_id, pop_id, timestamp)


def migrate_weights(tenant_id, csp_id):
    key = '%s:%s' % (tenant_id, csp_id)
    for weight_key in weight_keys:
        weight = source_redis.hget(weight_key, key)
        if weight is not None:
            dest_redis.hset(weight_key, key, weight)


def migrate_keys(keys):
    for key in keys:
        print('key = %s' % key)
        ttl = source_redis.ttl(key)
        if ttl < 0:
            ttl = 0
        print("Dumping key: %s" % key)
        value = source_redis.dump(key)
        if value is not None:
            print("Restoring key: %s" % key)
            try:
                dest_redis.delete(key)
                dest_redis.restore(key, ttl * 1000, value)
            except redis.exceptions.ResponseError:
                print("Failed to restore key: %s" % key)
                pass
        else:
            print('value of %s is none' % key)


def migrate_user_cache(tenant_id, csp_id, instance_id):
    user_cache_map_key = 'zeus:user-cache-map:%s:%s:%s' % (tenant_id, csp_id, instance_id)
    user_cache_set_key = 'zeus:user-cache:%s:%s:%s' % (tenant_id, csp_id, instance_id)
    iter_count = 1000
    if source_redis.exists(user_cache_map_key):
        print('Found user cache map key=%s, num entries=%s, migrating ...'
              % (user_cache_map_key, source_redis.hlen(user_cache_map_key)))
        itr_users_in_map = source_redis.hscan_iter(user_cache_map_key, match=None, count=iter_count)
        for key, val in itr_users_in_map:
            csp_q_dest_redis.hset(user_cache_map_key, key, val)

    if source_redis.exists(user_cache_set_key):
        print('Found user cache set key=%s, num entries=%s, migrating ...'
              % (user_cache_set_key, source_redis.scard(user_cache_set_key)))
        itr_users_in_set=source_redis.sscan_iter(user_cache_set_key, match=None, count=iter_count)
        for item in itr_users_in_set:
            csp_q_dest_redis.sadd(user_cache_set_key, item)
    elif csp_q_source_redis is not None:
        if csp_q_source_redis.exists(user_cache_map_key):
            print('Found user cache map key=%s, num entries=%s, migrating ...'
                  % (user_cache_map_key, csp_q_source_redis.hlen(user_cache_map_key)))
            itr_users_in_map = source_redis.hscan_iter(user_cache_map_key, match=None, count=iter_count)
            for key, val in itr_users_in_map:
                csp_q_dest_redis.hset(user_cache_map_key, key, val)
        if csp_q_source_redis.exists(user_cache_set_key):
            print('Found user cache set key=%s, num entries=%s, migrating ...'
                  % (user_cache_set_key, csp_q_source_redis.scard(user_cache_set_key)))
            itr_users_in_set=source_redis.sscan_iter(user_cache_set_key, match=None, count=iter_count)
            for item in itr_users_in_set:
                csp_q_dest_redis.sadd(user_cache_set_key, item)


def office365_keys(tenant_id, csp_id, instance_id):
    instance_key = '%s:%s:%s' % (tenant_id, csp_id, instance_id)
    user_checkpoint_key = 'dlp:user:cache:checkpoint'
    user_checkpoint_value = source_redis.hget(user_checkpoint_key, instance_key)
    print('')
    if user_checkpoint_value is not None:
        dest_redis.hset(user_checkpoint_key, instance_key, user_checkpoint_value)

    group_checkpoint_key = 'dlp:group:cache:checkpoint'
    group_checkpoint_value = source_redis.hget(group_checkpoint_key, instance_key)
    if group_checkpoint_value is not None:
        dest_redis.hset(group_checkpoint_key, instance_key, group_checkpoint_value)

    orgid_to_tenantinstance_key = 'offlinedlp:office365:orgids'
    org_id = source_redis.hget(orgid_to_tenantinstance_key, instance_key)
    if org_id is not None:
        dest_redis.hset(orgid_to_tenantinstance_key, instance_key, org_id)

    o365_keys = list()
    iter_count = 1000
    ingestion_pattern = 'dlp.user_ingestion_task:%s:*' % tenant_id
    user_ingestion_itr = source_redis.scan_iter(match=ingestion_pattern, count=iter_count)
    user_ingestion_key_expected_len = 3
    for k in user_ingestion_itr:
        user_ingestion_map = k.decode('utf8')
        if len(user_ingestion_map.split(':')) == user_ingestion_key_expected_len:
            if dest_redis.hlen(user_ingestion_map) == 0:
                print('found user ingestion map key: %s' % user_ingestion_map)
                o365_keys.append(user_ingestion_map)

    groups_cache_pattern = 'dlp:group:cache:groups:%s:*' % tenant_id
    itr_groups_cache_maps = source_redis.scan_iter(match=groups_cache_pattern, count=iter_count)
    groups_cache_key_expected_len = 6
    for k in itr_groups_cache_maps:
        groups_cache_map = k.decode('utf8')
        if len(groups_cache_map.split(':')) == groups_cache_key_expected_len:
            if dest_redis.hlen(groups_cache_map) == 0:
                print('found group cache map key: %s' % groups_cache_map)
                o365_keys.append(groups_cache_map)

    group_members_pattern = 'dlp:group:cache:members:%s:*' % tenant_id
    itr_group_members_keys = source_redis.scan_iter(match=group_members_pattern, count=iter_count)
    group_members_key_expected_len = 7
    for k in itr_group_members_keys:
        group_members_key = k.decode('utf8')
        if len(group_members_key.split(':')) == group_members_key_expected_len:
            if dest_redis.hlen(group_members_key) == 0:
                print('found group member key: %s' % group_members_key)
                o365_keys.append(group_members_key)

    group_member_email_pattern = 'dlp:group:cache:members:user_emails:%s:*' % tenant_id
    itr_group_member_email_maps = source_redis.scan_iter(match=group_member_email_pattern,
                                                         count=iter_count)
    group_member_email_key_expected_len = 8
    for k in itr_group_member_email_maps:
        group_member_email_key = k.decode('utf8')
        if len(group_member_email_key.split(':')) == group_member_email_key_expected_len:
            if dest_redis.hlen(group_member_email_key) == 0:
                print('found group member email key: %s' % group_member_email_key)
                o365_keys.append(group_member_email_key)

    group_member_group_ids_pattern = 'dlp:group:cache:members:group_ids:%s:*' % tenant_id
    itr_group_member_group_ids_maps = source_redis.scan_iter(match=group_member_group_ids_pattern,
                                                             count=iter_count)
    group_member_group_ids_key_expected_len = 8
    for k in itr_group_member_group_ids_maps:
        group_member_group_ids_key = k.decode('utf8')
        if len(group_member_group_ids_key.split(':')) == group_member_group_ids_key_expected_len:
            if dest_redis.hlen(group_member_group_ids_key) == 0:
                print('found group member group ids key: %s' % group_member_group_ids_key)
                o365_keys.append(group_member_group_ids_key)

    mgmt_feed_checkpoint_pattern = 'offlinedlp:office365-management-feed-checkpoint:%s:*:%s' \
                                   % (tenant_id, csp_id)
    mgmt_feed_checkpoint_keys = source_redis.scan_iter(match=mgmt_feed_checkpoint_pattern,
                                                       count=iter_count)
    for mgmt_key in mgmt_feed_checkpoint_keys:
        print('found management feed checkpoint key: %s' % mgmt_key)
        o365_keys.append(mgmt_key.decode('utf8'))

    mgmt_feed_checkpoint_pattern_azr = 'offlinedlp:office365-management-feed-checkpoint:%s:*:%s' \
                                       % (tenant_id, 12548)
    mgmt_feed_checkpoint_keys_azr = source_redis.scan_iter(match=mgmt_feed_checkpoint_pattern_azr,
                                                           count=iter_count)
    for mgmt_key in mgmt_feed_checkpoint_keys_azr:
        print('found azure management feed checkpoint key: %s' % mgmt_key)
        o365_keys.append(mgmt_key.decode('utf8'))

    o365_keys.append('offlinedlp:user-index-checkpoint:onedrive:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))
    o365_keys.append('offlinedlp:root_folder_index_task:%s:%s:%s:last_indexed_at'
                      % (tenant_id, csp_id, instance_id))
    print('total keys count: %s' % len(o365_keys))
    migrate_keys(o365_keys)


def slack_keys(tenant_id, csp_id, instance_id):
    slack = list()
    iter_count = 1000
    slack_channels_checkpoint_pattern = 'offlinedlp:slack:%s:channels:*' % tenant_id
    channel_checkpoint_keys = source_redis.scan_iter(match=slack_channels_checkpoint_pattern,
                                                       count=iter_count)
    for checkpoint_key in channel_checkpoint_keys:
        print('found slack channel checkpoint key: %s' % checkpoint_key)
        slack.append(checkpoint_key.decode('utf8'))

    slack_groups_checkpoint_pattern = 'offlinedlp:slack:%s:groups:*' % tenant_id
    slack_groups_checkpoint_keys = source_redis.scan_iter(match=slack_groups_checkpoint_pattern,
                                                          count=iter_count)
    for checkpoint_key in slack_groups_checkpoint_keys:
        print('found slack group checkpoint key: %s' % checkpoint_key)
        slack.append(checkpoint_key.decode('utf8'))

    slack_dms_checkpoint_pattern = 'offlinedlp:slack:%s:dms:*' % tenant_id
    slack_dms_checkpoint_keys = source_redis.scan_iter(match=slack_dms_checkpoint_pattern,
                                                       count=iter_count)
    for checkpoint_key in slack_dms_checkpoint_keys:
        print('found slack dms checkpoint key: %s' % checkpoint_key)
        slack.append(checkpoint_key.decode('utf8'))

    groups_key_pattern = 'offlinedlp:slack:*:groups'
    groups = source_redis.scan_iter(match=groups_key_pattern,
                                    count=iter_count)
    for group in groups:
        print('found group key: %s' % group)
        slack.append(group.decode('utf8'))

    message_offset_checkpoint_pattern = 'offlinedlp:slack:messages:%s:%s:*' \
                                        % (tenant_id, instance_id)
    message_offset_checkpoint_keys = source_redis.scan_iter(match=message_offset_checkpoint_pattern,
                                                            count=iter_count)
    for checkpoint_key in message_offset_checkpoint_keys:
        print('found message offset checkpoint key: %s' % checkpoint_key)
        slack.append(checkpoint_key.decode('utf8'))

    slack.append('offlinedlp:slack:files:%s:%s' % (tenant_id, instance_id))
    slack.append('slackdlpbot:::%s:::%s' % (tenant_id, csp_id))
    slack.append('offlinedlp:slack:polltriggeredtime:%s' % tenant_id)
    slack.append('offlinedlp:slack:channelhistorylatency:%s' % tenant_id)
    slack.append('offlinedlp:slack:slackpollinglatency:%s' % tenant_id)
    slack.append('offlinedlp:slack:slackhistorycheckpoint:%s' % tenant_id)
    migrate_keys(slack)


def get_keys(tenant_id, csp_id, instance_id):
    iter_count = 1000
    redis_keys = list()

    redis_keys.append('offlinedlp:event-poll-checkpoint:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))
    redis_keys.append('offlinedlp:last-registered-event-checkpoint:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))
    redis_keys.append('offlinedlp:registered-events:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))
    redis_keys.append('offlinedlp:registered-events:ordered:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))
    redis_keys.append('offlinedlp:activity-poll-checkpoint:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))
    redis_keys.append('offlinedlp:last-activity-registered-checkpoint:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))
    redis_keys.append('offlinedlp:last-event-poll-time:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))
    redis_keys.append('dlp.self_remediation:%s:%s:%s'
                      % (tenant_id, csp_id, instance_id))

    migrate_keys(redis_keys)

    if csp_id == 2483:
        redis_keys.clear()
        redis_keys.append('dlp:dedupe:index:login:%s:%s:%s'
                        % (tenant_id, csp_id, instance_id))
        redis_keys.append('dlp:dedupe:index:admin:%s:%s:%s'
                        % (tenant_id, csp_id, instance_id))
        # should the below keys go into csp_q_dest_redis ???
        redis_keys.append('dlp.drive_user_ingestion_task:%s:%s:%s'
                        % (tenant_id, csp_id, instance_id))
        redis_keys.append('dlp:drive_group_email_name:%s:%s:%s'
                        % (tenant_id, csp_id, instance_id))
        redis_keys.append('dlp:drive_group:%s:%s:%s'
                        % (tenant_id, csp_id, instance_id))
        # should the above keys go into csp_q_dest_redis ???
        migrate_keys(redis_keys)
    elif csp_id == 3210 or csp_id == 16131:
        office365_keys(tenant_id, csp_id, instance_id)
    elif csp_id == 4366:
        redis_keys.clear()
        checkpoint_pattern = 'offlinedlp:azure-activity-log-checkpoint:%s:%s:%s:*' \
            % (tenant_id, csp_id, instance_id)
        azure_checkpoint_itr = source_redis.scan_iter(match=checkpoint_pattern,
                                                               count=iter_count)
        for checkpoint in azure_checkpoint_itr:
            print('found azure activity log checkpoint key: %s' % checkpoint)
            redis_keys.append(checkpoint.decode('utf8'))
        migrate_keys(redis_keys)
    elif csp_id == 2941:
        redis_keys.clear()
        redis_keys.append('offlinedlp:sfdc:logscheckpoint:%s:%s' % (tenant_id, instance_id))
        migrate_keys(redis_keys)
    elif csp_id == 10692:
        redis_keys.clear()
        redis_keys.append('offlinedlp:slack:active_channels_after:%s:%s:%s'
                        % (tenant_id, csp_id, instance_id))
        redis_keys.append('offlinedlp:slack:external-channels:%s:%s:%s'
                        % (tenant_id, csp_id, instance_id))
        redis_keys.append('offlinedlp:slack:auditlog')
        redis_keys.append('dlp.slack_tasks:%s:%s:%s:conversations_recent'
                        % (tenant_id, csp_id, instance_id))
        redis_keys.append('dlp.slack_tasks:%s:%s:%s:conversations_history'
                        % (tenant_id, csp_id, instance_id))
        redis_keys.append('dlp.slack_tasks:%s:%s:%s:files_list'
                        % (tenant_id, csp_id, instance_id))
        migrate_keys(redis_keys)
        slack_keys(tenant_id, csp_id, instance_id)
    elif csp_id == 3545:
        redis_keys.clear()
        redis_keys.append('offlinedlp:object_index_task:%s:%s:%s:last_indexed_at'
                          % (tenant_id, csp_id, instance_id))
        redis_keys.append('offlinedlp:structured_csp:nrt_config:dynamics:service_endpoint:%s:%s:%s'
                          % (tenant_id, csp_id, instance_id))
        redis_keys.append('offlinedlp:structured_csp:nrt_config:%s:%s:%s'
                          % (tenant_id, csp_id, instance_id))
        migrate_keys(redis_keys)
    elif csp_id == 3487:
        redis_keys.clear()
        redis_keys.append('offlinedlp:object_index_task:%s:%s:%s:last_indexed_at'
                          % (tenant_id, csp_id, instance_id))
        migrate_keys(redis_keys)
    elif csp_id in casb_polling_csps:
        redis_keys.clear()
        redis_keys.append('offlinedlp:generic:connector:%s:%s:%s'
                          % (tenant_id, csp_id, instance_id))
        migrate_keys(redis_keys)


def if_not_already_moved_move_slack_csp_level_keys():
    ignore_time_stamps_key = 'offlinedlp:slack:ignoretimestamps'
    if not dest_redis.exists(ignore_time_stamps_key):
        keys = list()
        keys.append('offlinedlp:slack:logfile-event-category:Service Usage')
        keys.append('offlinedlp:slack:logfile-event-category:Data Download')
        keys.append('offlinedlp:slack:logfile-event-category:User Account Deletion')
        keys.append('offlinedlp:slack:logfile-event-category:User Account Creation')
        keys.append('offlinedlp:slack:logfile-event-category:Data Delete')
        keys.append('offlinedlp:slack:logfile-event-category:Data Upload')
        keys.append('offlinedlp:slack:logfile-event-category:Login Success')
        keys.append('offlinedlp:slack:logfile-event-category:Data Sharing')
        keys.append('offlinedlp:slack:logfile-event-category:Administration')
        keys.append('offlinedlp:slack:logfile-event-category:External Data Sharing')
        keys.append('offlinedlp:slack:logfile-event-category:Report Execution')
        migrate_keys(keys)


def print_running_time(start_time, sleep_time):
    while True:
        duration = (time.time() - start_time)/60
        print('\n running for {:.2f} minutes ...'.format(duration))
        time.sleep(sleep_time)


def migrate():
    sleep_time = 20
    x = threading.Thread(target=print_running_time, args=(time.time(), sleep_time, ), daemon=True)
    x.start()
    name_to_id_map = get_pop_name_to_id_map()
    if name_to_id_map.get(destination_pop.lower()) is None:
        print('Invalid Destination POP %s' % destination_pop)
        return
    dest_pop_id = name_to_id_map[destination_pop.lower()]
    print("Migrating instance to POP id=%d" % dest_pop_id)

    instances = get_instance_details()

    for instance in instances:
        tenant_id = instance['tenant_id']
        csp_id = instance['csp_id']
        instance_id = instance['instance_id']
        # soft disable only if not a webhook based csp
        is_api_access_soft_disabled = False
        if csp_id not in casb_webhook_csps:
            api_access_disable_soft(instance_id)
            print('Disabled API access for instance_ids=%s' % instance_id)
            print('Sleeping for %s seconds to make sure source pop stops polling' % sleep_time)
            time.sleep(sleep_time)
            is_api_access_soft_disabled = True
            print('Done sleeping, migrating data')
        get_keys(tenant_id, csp_id, instance_id)
        migrate_user_cache(tenant_id, csp_id, instance_id)
        print("Migrated redis keys for tenant_id=%s csp_id=%s instance_id=%s"
              % (tenant_id, csp_id, instance_id))
        migrate_weights(tenant_id, csp_id)
        print("Migrated weights for tenant_id=%s csp_id=%s" % (tenant_id, csp_id))
        update_dlp_zeus_group_csp_mapping(tenant_id, csp_id, instance_id, dest_pop_id)
        print("Updated zeus group mapping for instance=%d" % instance_id)
        # soft enable only if not a webhook based csp
        if is_api_access_soft_disabled:
            api_access_enable_soft(instance_id)
            print('Enabled API access for instance_ids=%s' % instance_id)
    if_not_already_moved_move_slack_csp_level_keys()
    print("Done migrating all the instances. Migration complete!")


migrate()
