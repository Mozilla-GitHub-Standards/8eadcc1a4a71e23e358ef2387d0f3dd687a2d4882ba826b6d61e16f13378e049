"""
This is the body of the lambda function for the Auth0 Identity driver of CIS
This function retrieves user profiles from the CIS ID Vault and sends the appropriate data to the Auth0 API
which is in turn used to create the id_token JWT and fill the user info endpoint ('profile' scope)
"""
import authzero
import boto3
import credstash
import logging
import os


from botocore.exceptions import ClientError
from cis.libs import utils


def find_user(user_id):
    table_name = os.getenv('CIS_DYNAMODB_TABLE', None)
    dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
    table = dynamodb.Table(table_name)
    try:
        res = table.get_item(
            Key={
                'user_id': user_id
            }
        )
        profile = res.get('Item', None)

        # Fix null values workaround for DynamoDB limitation
        if profile and profile['groups'] == 'NULL':
            profile['groups'] = []

        return profile

    except ClientError:
        return None


def handle(event, context):
    utils.StructuredLogger(
        name='cis-idvtoauth0',
        level=logging.INFO
    )

    logger = logging.getLogger('cis-idvtoauth0')
    logger.info("Stream Processor initialized.")

    environment = os.getenv('ENVIRONMENT', 'dev')

    if environment == 'production':
        environment = 'prod'
    else:
        logger.info('Development stage recognized.  Applying to credstash.')
        environment = 'dev'

    # New up the config object for CISAuthZero
    config = authzero.DotDict(dict())
    config.client_id = credstash.getSecret(
        name="cis.client_id",
        context={'app': 'cis', 'environment': environment},
        region="us-west-2"
    )

    config.client_secret = credstash.getSecret(
        name="cis.client_secret",
        context={'app': 'cis', 'environment': environment},
        region="us-west-2"
    )

    config.uri = credstash.getSecret(
        name="cis.uri",
        context={'app': 'cis', 'environment': environment},
        region="us-west-2"
    )

    client = authzero.CISAuthZero(config)
    client.get_access_token()

    for record in event['Records']:
        # Kinesis data is base64 encoded so decode here
        logger.info("Record is loaded.")
        logger.info("Processing {record}".format(record=record))
        user_id = record['dynamodb']['Keys']['user_id']['S']

        logger.info("Initial payload decoded.")
        logger.info("Searching for dynamo record for {u}".format(u=user_id))
        profile = find_user(user_id)

        logger.info("Status of profile search is {s}".format(s=profile))

        if profile is not None:
            logger.info("The profile is {}".format(profile))
            try:
                upstream_user = client.get_user(user_id)

                # XXX Attempt forced LDAP group reintegration
                # Remove when we have an LDAP CIS Publisher
                # And replace with a "user add" and "user remove/block" functionality
                if 'groups' in upstream_user.keys():
                    for g in upstream_user['groups']:
                        if g not in profile['groups']:
                            profile['groups'].append(g)
                            logger.info("Forced re-integration of LDAP group {}".format(g))

                # Update groups only in Auth0
                profile_groups = {'groups': profile.get('groups')}
                res = client.update_user(user_id, profile_groups)
                logger.info("Updating user group information in auth0 for {user_id}".format(user_id=user_id))
            except Exception as e:
                """Temporarily patch around raising inside loop until authzero.py can become part of CIS core."""
                res = e
            logger.info("Status of message processing is {s}".format(s=res))
        else:
            logger.critical(
                "User could not be matched in vault for userid : {user_id}".format(user_id=user_id)
            )

    logger.info(
        'Successfully processed {} records.'.format(len(event['Records']))
    )

    return 'Successfully processed {} records.'.format(len(event['Records']))
