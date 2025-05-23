import abc
import base64
import copy
import datetime
import hashlib
import json
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Union

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from localstack import config
from localstack.aws.api.lambda_ import InvocationType
from localstack.aws.api.sns import MessageAttributeMap
from localstack.aws.connect import connect_to
from localstack.config import external_service_url
from localstack.services.sns import constants as sns_constants
from localstack.services.sns.certificate import SNS_SERVER_PRIVATE_KEY
from localstack.services.sns.executor import TopicPartitionedThreadPoolExecutor
from localstack.services.sns.filter import SubscriptionFilter
from localstack.services.sns.models import (
    SnsApplicationPlatforms,
    SnsMessage,
    SnsMessageType,
    SnsStore,
    SnsSubscription,
)
from localstack.utils.aws.arns import (
    PARTITION_NAMES,
    extract_account_id_from_arn,
    extract_region_from_arn,
    extract_resource_from_arn,
    parse_arn,
    sqs_queue_url_for_arn,
)
from localstack.utils.aws.aws_responses import create_sqs_system_attributes
from localstack.utils.aws.client_types import ServicePrincipal
from localstack.utils.aws.dead_letter_queue import sns_error_to_dead_letter_queue
from localstack.utils.bootstrap import is_api_enabled
from localstack.utils.cloudwatch.cloudwatch_util import store_cloudwatch_logs
from localstack.utils.objects import not_none_or
from localstack.utils.strings import long_uid, md5, to_bytes, to_str
from localstack.utils.time import timestamp_millis

LOG = logging.getLogger(__name__)


@dataclass
class SnsPublishContext:
    message: SnsMessage
    store: SnsStore
    request_headers: dict[str, str]
    topic_attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class SnsBatchPublishContext:
    messages: List[SnsMessage]
    store: SnsStore
    request_headers: Dict[str, str]
    topic_attributes: dict[str, str] = field(default_factory=dict)


class TopicPublisher(abc.ABC):
    """
    The TopicPublisher is responsible for publishing SNS messages to a topic's subscription.
    This is the base class implementing the basic logic.
    Each subclass will need to implement `_publish` using the subscription's protocol logic and client.
    Subclasses can override `prepare_message` if the format of the message is different.
    """

    def publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        """
        This function wraps the underlying call to the actual publishing. This allows us to catch any uncaught
        exception and log it properly. This method is passed to the ThreadPoolExecutor, which would swallow the
        exception. This is a convenient way of doing it, but not something the abstract class should take care.
        Discussion here: https://github.com/localstack/localstack/pull/7267#discussion_r1056873437
        # TODO: move this out of the base class
        :param context: the SnsPublishContext created by the caller, containing the necessary data to publish the
        message
        :param subscriber: the subscription data
        :return:
        """
        try:
            self._publish(context=context, subscriber=subscriber)
        except Exception:
            LOG.exception(
                "An internal error occurred while trying to send the SNS message %s",
                context.message,
            )
            return

    def _publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        """
        Base method for publishing the message. It is up to the child class to implement its way to publish the message
        :param context: the SnsPublishContext created by the caller, containing the necessary data to publish the
        message
        :param subscriber: the subscription data
        :return:
        """
        raise NotImplementedError

    def prepare_message(
        self,
        message_context: SnsMessage,
        subscriber: SnsSubscription,
        topic_attributes: dict[str, str] = None,
    ) -> str:
        """
        Returns the message formatted in the base SNS message format. The base SNS message format is shared amongst
        SQS, HTTP(S), email-json and Firehose.
        See https://docs.aws.amazon.com/sns/latest/dg/sns-sqs-as-subscriber.html
        :param message_context: the SnsMessage containing the message data
        :param subscriber: the SNS subscription
        :param topic_attributes: the SNS Topic attributes
        :return: formatted SNS message body in a JSON string
        """
        return create_sns_message_body(message_context, subscriber, topic_attributes)


class EndpointPublisher(abc.ABC):
    """
    The EndpointPublisher is responsible for publishing SNS messages directly to an endpoint.
    SNS allows directly publishing to phone numbers and application endpoints.
    This is the base class implementing the basic logic.
    Each subclass will need to implement `_publish` and `prepare_message `using the subscription's protocol logic
    and client.
    """

    def publish(self, context: SnsPublishContext, endpoint: str):
        """
        This function wraps the underlying call to the actual publishing. This allows us to catch any uncaught
        exception and log it properly. This method is passed to the ThreadPoolExecutor, which would swallow the
        exception. This is a convenient way of doing it, but not something the abstract class should take care.
        Discussion here: https://github.com/localstack/localstack/pull/7267#discussion_r1056873437
        # TODO: move this out of the base class
        :param context: the SnsPublishContext created by the caller, containing the necessary data to publish the
        message
        :param endpoint: the endpoint where the message should be published
        :return:
        """
        try:
            self._publish(context=context, endpoint=endpoint)
        except Exception:
            LOG.exception(
                "An internal error occurred while trying to send the SNS message %s",
                context.message,
            )
            return

    def _publish(self, context: SnsPublishContext, endpoint: str):
        """
        Base method for publishing the message. It is up to the child class to implement its way to publish the message
        :param context: the SnsPublishContext created by the caller, containing the necessary data to publish the
        message
        :param endpoint: the endpoint where the message should be published
        :return:
        """
        raise NotImplementedError

    def prepare_message(self, message_context: SnsMessage, endpoint: str) -> str:
        """
        Base method to format the message. It is up to the child class to implement it.
        :param message_context: the SnsMessage containing the message data
        :param endpoint: the endpoint where the message should be published
        :return: the formatted message
        """
        raise NotImplementedError


class LambdaTopicPublisher(TopicPublisher):
    """
    The Lambda publisher is responsible for invoking a subscribed lambda function to process the SNS message using
    `Lambda.invoke` with the formatted message as Payload.
    See: https://docs.aws.amazon.com/lambda/latest/dg/with-sns.html
    """

    def _publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        try:
            region = extract_region_from_arn(subscriber["Endpoint"])
            lambda_client = connect_to(region_name=region).lambda_.request_metadata(
                source_arn=subscriber["TopicArn"], service_principal="sns"
            )
            event = self.prepare_message(context.message, subscriber, context.topic_attributes)
            inv_result = lambda_client.invoke(
                FunctionName=subscriber["Endpoint"],
                Payload=to_bytes(event),
                InvocationType=InvocationType.Event,
            )
            status_code = inv_result.get("StatusCode")
            payload = inv_result.get("Payload")
            if payload:
                delivery = {
                    "statusCode": status_code,
                    "providerResponse": json.dumps(
                        {"lambdaRequestId": inv_result["ResponseMetadata"]["RequestId"]}
                    ),
                }
                store_delivery_log(
                    context.message,
                    subscriber,
                    success=True,
                    topic_attributes=context.topic_attributes,
                    delivery=delivery,
                )

        except Exception as exc:
            LOG.info(
                "Unable to run Lambda function on SNS message: %s %s", exc, traceback.format_exc()
            )
            store_delivery_log(
                context.message,
                subscriber,
                success=False,
                topic_attributes=context.topic_attributes,
            )
            message_body = create_sns_message_body(
                message_context=context.message,
                subscriber=subscriber,
                topic_attributes=context.topic_attributes,
            )
            sns_error_to_dead_letter_queue(subscriber, message_body, str(exc))

    def prepare_message(
        self,
        message_context: SnsMessage,
        subscriber: SnsSubscription,
        topic_attributes: dict[str, str] = None,
    ) -> str:
        """
        You can see Lambda SNS Event format here: https://docs.aws.amazon.com/lambda/latest/dg/with-sns.html
        :param message_context: the SnsMessage containing the message data
        :param subscriber: the SNS subscription
        :return: an SNS message body formatted as a lambda Event in a JSON string
        """
        external_url = get_cert_base_url()
        unsubscribe_url = create_unsubscribe_url(external_url, subscriber["SubscriptionArn"])
        message_attributes = prepare_message_attributes(message_context.message_attributes)

        event_payload = {
            "Type": message_context.type or SnsMessageType.Notification,
            "MessageId": message_context.message_id,
            "Subject": message_context.subject,
            "TopicArn": subscriber["TopicArn"],
            "Message": message_context.message_content(subscriber["Protocol"]),
            "Timestamp": timestamp_millis(),
            "UnsubscribeUrl": unsubscribe_url,
            "MessageAttributes": message_attributes,
        }

        signature_version = (
            topic_attributes.get("signature_version", "1") if topic_attributes else "1"
        )
        canonical_string = compute_canonical_string(event_payload, message_context.type)
        signature = get_message_signature(canonical_string, signature_version=signature_version)

        event_payload.update(
            {
                # this is a bug on AWS side, it is always returned a 1, but it should be actual version of the topic
                "SignatureVersion": "1",
                "Signature": signature,
                "SigningCertUrl": f"{external_url}{sns_constants.SNS_CERT_ENDPOINT}",
            }
        )
        event = {
            "Records": [
                {
                    "EventSource": "aws:sns",
                    "EventVersion": "1.0",
                    "EventSubscriptionArn": subscriber["SubscriptionArn"],
                    "Sns": event_payload,
                }
            ]
        }
        return json.dumps(event)


class SqsTopicPublisher(TopicPublisher):
    """
    The SQS publisher is responsible for publishing the SNS message to a subscribed SQS queue using `SQS.send_message`.
    For integrations and the format of message, see:
    https://docs.aws.amazon.com/sns/latest/dg/sns-sqs-as-subscriber.html
    """

    def _publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        message_context = context.message
        try:
            message_body = self.prepare_message(
                message_context, subscriber, topic_attributes=context.topic_attributes
            )
            kwargs = self.get_sqs_kwargs(msg_context=message_context, subscriber=subscriber)
        except Exception:
            LOG.exception("An internal error occurred while trying to format the message for SQS")
            return
        try:
            queue_url: str = sqs_queue_url_for_arn(subscriber["Endpoint"])
            region = extract_region_from_arn(subscriber["Endpoint"])
            sqs_client = connect_to(region_name=region).sqs.request_metadata(
                source_arn=subscriber["TopicArn"], service_principal="sns"
            )
            sqs_client.send_message(
                QueueUrl=queue_url,
                MessageBody=message_body,
                MessageSystemAttributes=create_sqs_system_attributes(context.request_headers),
                **kwargs,
            )
            store_delivery_log(
                message_context, subscriber, success=True, topic_attributes=context.topic_attributes
            )
        except Exception as exc:
            LOG.info("Unable to forward SNS message to SQS: %s %s", exc, traceback.format_exc())
            store_delivery_log(
                message_context,
                subscriber,
                success=False,
                topic_attributes=context.topic_attributes,
            )
            sns_error_to_dead_letter_queue(subscriber, message_body, str(exc), **kwargs)
            if "NonExistentQueue" in str(exc):
                LOG.debug("The SQS queue endpoint does not exist anymore")
                # todo: if the queue got deleted, even if we recreate a queue with the same name/url
                #  AWS won't send to it anymore. Would need to unsub/resub.
                #  We should mark this subscription as "broken"

    @staticmethod
    def get_sqs_kwargs(msg_context: SnsMessage, subscriber: SnsSubscription):
        kwargs = {}
        if is_raw_message_delivery(subscriber) and msg_context.message_attributes:
            kwargs["MessageAttributes"] = msg_context.message_attributes

        # SNS now allows regular non-fifo subscriptions to FIFO topics. Validate that the subscription target is fifo
        # before passing the FIFO-only parameters
        if subscriber["Endpoint"].endswith(".fifo"):
            if msg_context.message_group_id:
                kwargs["MessageGroupId"] = msg_context.message_group_id
            if msg_context.message_deduplication_id:
                kwargs["MessageDeduplicationId"] = msg_context.message_deduplication_id
            elif subscriber["TopicArn"].endswith(".fifo"):
                # Amazon SNS uses the message body provided to generate a unique hash value to use as the deduplication
                # ID for each message, so you don't need to set a deduplication ID when you send each message.
                # https://docs.aws.amazon.com/sns/latest/dg/fifo-message-dedup.html
                content = msg_context.message_content("sqs")
                kwargs["MessageDeduplicationId"] = hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest()

        # TODO: for message deduplication, we are using the underlying features of the SQS queue
        # however, SQS queue only deduplicate at the Queue level, where the SNS topic deduplicate on the topic level
        # we will need to implement this
        return kwargs


class SqsBatchTopicPublisher(SqsTopicPublisher):
    """
    The SQS Batch publisher is responsible for publishing batched SNS messages to a subscribed SQS queue using
    `SQS.send_message_batch`. This allows to make use of SQS batching capabilities.
    See https://docs.aws.amazon.com/sns/latest/dg/sns-batch-api-actions.html
    https://docs.aws.amazon.com/sns/latest/api/API_PublishBatch.html
    https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessageBatch.html
    """

    def _publish(self, context: SnsBatchPublishContext, subscriber: SnsSubscription):
        entries = []
        sqs_system_attrs = create_sqs_system_attributes(context.request_headers)
        # TODO: check ID, SNS rules are not the same as SQS, so maybe generate the entries ID
        failure_map = {}
        for index, message_ctx in enumerate(context.messages):
            message_body = self.prepare_message(
                message_ctx, subscriber, topic_attributes=context.topic_attributes
            )
            sqs_kwargs = self.get_sqs_kwargs(message_ctx, subscriber)
            entry = {"Id": f"sns-batch-{index}", "MessageBody": message_body, **sqs_kwargs}
            # in case of failure
            failure_map[entry["Id"]] = {
                "context": message_ctx,
                "entry": entry,
            }

            if sqs_system_attrs:
                entry["MessageSystemAttributes"] = sqs_system_attrs

            entries.append(entry)

        try:
            queue_url = sqs_queue_url_for_arn(subscriber["Endpoint"])

            account_id = extract_account_id_from_arn(subscriber["Endpoint"])
            region = extract_region_from_arn(subscriber["Endpoint"])

            sqs_client = connect_to(
                aws_access_key_id=account_id, region_name=region
            ).sqs.request_metadata(source_arn=subscriber["TopicArn"], service_principal="sns")
            response = sqs_client.send_message_batch(QueueUrl=queue_url, Entries=entries)

            for message_ctx in context.messages:
                store_delivery_log(
                    message_ctx, subscriber, success=True, topic_attributes=context.topic_attributes
                )

            if failed_messages := response.get("Failed"):
                for failed_msg in failed_messages:
                    failure_data = failure_map.get(failed_msg["Id"])
                    LOG.info(
                        "Unable to forward SNS message to SQS: %s %s",
                        failed_msg["Code"],
                        failed_msg["Message"],
                    )
                    store_delivery_log(
                        failure_data["context"],
                        subscriber,
                        success=False,
                        topic_attributes=context.topic_attributes,
                    )
                    kwargs = {}
                    if msg_attrs := failure_data["entry"].get("MessageAttributes"):
                        kwargs["MessageAttributes"] = msg_attrs

                    if msg_group_id := failure_data["context"].get("MessageGroupId"):
                        kwargs["MessageGroupId"] = msg_group_id

                    if msg_dedup_id := failure_data["context"].get("MessageDeduplicationId"):
                        kwargs["MessageDeduplicationId"] = msg_dedup_id

                    sns_error_to_dead_letter_queue(
                        sns_subscriber=subscriber,
                        message=failure_data["entry"]["MessageBody"],
                        error=failed_msg["Code"],
                        **kwargs,
                    )

        except Exception as exc:
            LOG.info("Unable to forward SNS message to SQS: %s %s", exc, traceback.format_exc())
            for message_ctx in context.messages:
                store_delivery_log(
                    message_ctx,
                    subscriber,
                    success=False,
                    topic_attributes=context.topic_attributes,
                )
                msg_body = self.prepare_message(
                    message_ctx, subscriber, topic_attributes=context.topic_attributes
                )
                kwargs = self.get_sqs_kwargs(message_ctx, subscriber)

                sns_error_to_dead_letter_queue(
                    subscriber,
                    msg_body,
                    str(exc),
                    **kwargs,
                )
            if "NonExistentQueue" in str(exc):
                LOG.debug("The SQS queue endpoint does not exist anymore")
                # todo: if the queue got deleted, even if we recreate a queue with the same name/url
                #  AWS won't send to it anymore. Would need to unsub/resub.
                #  We should mark this subscription as "broken"


class HttpTopicPublisher(TopicPublisher):
    """
    The HTTP(S) publisher is responsible for publishing the SNS message to an external HTTP(S) endpoint which subscribed
    to the topic. It will create an HTTP POST request to be sent to the endpoint.
    See https://docs.aws.amazon.com/sns/latest/dg/sns-http-https-endpoint-as-subscriber.html
    """

    def _publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        message_context = context.message
        message_body = self.prepare_message(
            message_context, subscriber, topic_attributes=context.topic_attributes
        )
        try:
            message_headers = {
                "Content-Type": "text/plain; charset=UTF-8",
                "Accept-Encoding": "gzip,deflate",
                "User-Agent": "Amazon Simple Notification Service Agent",
                # AWS headers according to
                # https://docs.aws.amazon.com/sns/latest/dg/sns-message-and-json-formats.html#http-header
                "x-amz-sns-message-type": message_context.type,
                "x-amz-sns-message-id": message_context.message_id,
                "x-amz-sns-topic-arn": subscriber["TopicArn"],
            }
            if message_context.type != SnsMessageType.SubscriptionConfirmation:
                # while testing, never had those from AWS but the docs above states it should be there
                message_headers["x-amz-sns-subscription-arn"] = subscriber["SubscriptionArn"]

                # When raw message delivery is enabled, x-amz-sns-rawdelivery needs to be set to 'true'
                # indicating that the message has been published without JSON formatting.
                # https://docs.aws.amazon.com/sns/latest/dg/sns-large-payload-raw-message-delivery.html
                if message_context.type == SnsMessageType.Notification:
                    if is_raw_message_delivery(subscriber):
                        message_headers["x-amz-sns-rawdelivery"] = "true"
                    if content_type := self._get_content_type(subscriber, context.topic_attributes):
                        message_headers["Content-Type"] = content_type

            response = requests.post(
                subscriber["Endpoint"],
                headers=message_headers,
                data=message_body,
                verify=False,
            )

            delivery = {
                "statusCode": response.status_code,
                "providerResponse": response.content.decode("utf-8"),
            }
            store_delivery_log(
                message_context,
                subscriber,
                success=True,
                delivery=delivery,
                topic_attributes=context.topic_attributes,
            )

            response.raise_for_status()
        except Exception as exc:
            LOG.info(
                "Received error on sending SNS message, putting to DLQ (if configured): %s", exc
            )
            store_delivery_log(
                message_context,
                subscriber,
                success=False,
                topic_attributes=context.topic_attributes,
            )
            # AWS doesn't send to the DLQ if there's an error trying to deliver a UnsubscribeConfirmation msg
            if message_context.type != SnsMessageType.UnsubscribeConfirmation:
                sns_error_to_dead_letter_queue(subscriber, message_body, str(exc))

    @staticmethod
    def _get_content_type(subscriber: SnsSubscription, topic_attributes: dict) -> str | None:
        # TODO: we need to load the DeliveryPolicy every time if there's one, we should probably save the loaded
        #  policy on the subscription and dumps it when requested instead
        # to be much faster, once the logic is implemented in moto, we would only need to fetch EffectiveDeliveryPolicy,
        # which would already have the value from the topic
        if json_sub_delivery_policy := subscriber.get("DeliveryPolicy"):
            sub_delivery_policy = json.loads(json_sub_delivery_policy)
            if sub_content_type := sub_delivery_policy.get("requestPolicy", {}).get(
                "headerContentType"
            ):
                return sub_content_type

        if json_topic_delivery_policy := topic_attributes.get("delivery_policy"):
            topic_delivery_policy = json.loads(json_topic_delivery_policy)
            if not (
                topic_content_type := topic_delivery_policy.get(subscriber["Protocol"].lower())
            ):
                return
            if content_type := topic_content_type.get("defaultRequestPolicy", {}).get(
                "headerContentType"
            ):
                return content_type


class EmailJsonTopicPublisher(TopicPublisher):
    """
    The email-json publisher is responsible for publishing the SNS message to a subscribed email address.
    The format of the message will be JSON-encoded, and "is meant for applications to programmatically process emails".
    There is not a lot of AWS documentation on SNS emails.
    See https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html
    But it is mentioned several times in the SNS FAQ (especially in #Transports section):
    https://aws.amazon.com/sns/faqs/
    """

    def _publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        account_id = extract_account_id_from_arn(subscriber["Endpoint"])
        region = extract_region_from_arn(subscriber["Endpoint"])
        ses_client = connect_to(aws_access_key_id=account_id, region_name=region).ses
        if endpoint := subscriber.get("Endpoint"):
            # TODO: legacy value, replace by a more sane value in the future
            #  no-reply@sns-localstack.cloud or similar
            sender = config.SNS_SES_SENDER_ADDRESS or "admin@localstack.com"
            ses_client.verify_email_address(EmailAddress=endpoint)
            ses_client.verify_email_address(EmailAddress=sender)
            message_body = self.prepare_message(
                context.message, subscriber, topic_attributes=context.topic_attributes
            )
            ses_client.send_email(
                Source=sender,
                Message={
                    "Body": {"Text": {"Data": message_body}},
                    "Subject": {"Data": "SNS-Subscriber-Endpoint"},
                },
                Destination={"ToAddresses": [endpoint]},
            )


class EmailTopicPublisher(EmailJsonTopicPublisher):
    """
    The email publisher is responsible for publishing the SNS message to a subscribed email address.
    The format of the message will be text-based, and "is meant for end-users/consumers and notifications are regular,
     text-based messages which are easily readable."
    See https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html
    """

    def prepare_message(
        self,
        message_context: SnsMessage,
        subscriber: SnsSubscription,
        topic_attributes: dict[str, str] = None,
    ) -> str:
        return message_context.message_content(subscriber["Protocol"])


class ApplicationTopicPublisher(TopicPublisher):
    """
    The application publisher is responsible for publishing the SNS message to a subscribed SNS application endpoint.
    The SNS application endpoint represents a mobile app and device.
    The application endpoint can be of different types, represented in `SnsApplicationPlatforms`.
    This is not directly implemented yet in LocalStack, we save the message to be retrieved later from an internal
    endpoint.
    The `LEGACY_SNS_GCM_PUBLISHING` flag allows direct publishing to the GCM platform, with some caveats:
    - It always publishes if the platform is GCM, and raises an exception if the credentials are wrong.
    - the Platform Application should be validated before and not while publishing
    See https://docs.aws.amazon.com/sns/latest/dg/sns-mobile-application-as-subscriber.html
    """

    def _publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        endpoint_arn = subscriber["Endpoint"]
        message = self.prepare_message(
            context.message, subscriber, topic_attributes=context.topic_attributes
        )
        cache = context.store.platform_endpoint_messages.setdefault(endpoint_arn, [])
        cache.append(message)

        if (
            config.LEGACY_SNS_GCM_PUBLISHING
            and get_platform_type_from_endpoint_arn(endpoint_arn) == "GCM"
        ):
            self._legacy_publish_to_gcm(context, endpoint_arn)

        # TODO: rewrite the platform application publishing logic
        #  will need to validate credentials when creating platform app earlier, need thorough testing

        store_delivery_log(
            context.message, subscriber, success=True, topic_attributes=context.topic_attributes
        )

    def prepare_message(
        self,
        message_context: SnsMessage,
        subscriber: SnsSubscription,
        topic_attributes: dict[str, str] = None,
    ) -> dict[str, str]:
        endpoint_arn = subscriber["Endpoint"]
        platform_type = get_platform_type_from_endpoint_arn(endpoint_arn)
        return {
            "TargetArn": endpoint_arn,
            "TopicArn": subscriber["TopicArn"],
            "SubscriptionArn": subscriber["SubscriptionArn"],
            "Message": message_context.message_content(protocol=platform_type),
            "MessageAttributes": message_context.message_attributes,
            "MessageStructure": message_context.message_structure,
            "Subject": message_context.subject,
        }

    @staticmethod
    def _legacy_publish_to_gcm(context: SnsPublishContext, endpoint: str):
        application_attributes, endpoint_attributes = get_attributes_for_application_endpoint(
            endpoint
        )
        send_message_to_gcm(
            context=context,
            app_attributes=application_attributes,
            endpoint_attributes=endpoint_attributes,
        )


class SmsTopicPublisher(TopicPublisher):
    """
    The SMS publisher is responsible for publishing the SNS message to a subscribed phone number.
    This is not directly implemented yet in LocalStack, we only save the message.
    # TODO: create an internal endpoint to retrieve SMS.
    """

    def _publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        event = self.prepare_message(
            context.message, subscriber, topic_attributes=context.topic_attributes
        )
        context.store.sms_messages.append(event)
        LOG.info(
            "Delivering SMS message to %s: %s from topic: %s",
            event["PhoneNumber"],
            event["Message"],
            event["TopicArn"],
        )

        # MOCK DATA
        delivery = {
            "phoneCarrier": "Mock Carrier",
            "mnc": 270,
            "priceInUSD": 0.00645,
            "smsType": "Transactional",
            "mcc": 310,
            "providerResponse": "Message has been accepted by phone carrier",
            "dwellTimeMsUntilDeviceAck": 200,
        }
        store_delivery_log(context.message, subscriber, success=True, delivery=delivery)

    def prepare_message(
        self,
        message_context: SnsMessage,
        subscriber: SnsSubscription,
        topic_attributes: dict[str, str] = None,
    ) -> dict:
        return {
            "PhoneNumber": subscriber["Endpoint"],
            "TopicArn": subscriber["TopicArn"],
            "SubscriptionArn": subscriber["SubscriptionArn"],
            "MessageId": message_context.message_id,
            "Message": message_context.message_content(protocol=subscriber["Protocol"]),
            "MessageAttributes": message_context.message_attributes,
            "MessageStructure": message_context.message_structure,
            "Subject": message_context.subject,
        }


class FirehoseTopicPublisher(TopicPublisher):
    """
    The Firehose publisher is responsible for publishing the SNS message to a subscribed Firehose delivery stream.
    This allows you to "fan out Amazon SNS notifications to Amazon Simple Storage Service (Amazon S3), Amazon Redshift,
    Amazon OpenSearch Service (OpenSearch Service), and to third-party service providers."
    See https://docs.aws.amazon.com/sns/latest/dg/sns-firehose-as-subscriber.html
    """

    def _publish(self, context: SnsPublishContext, subscriber: SnsSubscription):
        message_body = self.prepare_message(
            context.message, subscriber, topic_attributes=context.topic_attributes
        )
        try:
            region = extract_region_from_arn(subscriber["Endpoint"])
            if role_arn := subscriber.get("SubscriptionRoleArn"):
                factory = connect_to.with_assumed_role(
                    role_arn=role_arn, service_principal=ServicePrincipal.sns, region_name=region
                )
            else:
                account_id = extract_account_id_from_arn(subscriber["Endpoint"])
                factory = connect_to(aws_access_key_id=account_id, region_name=region)
            firehose_client = factory.firehose.request_metadata(
                source_arn=subscriber["TopicArn"], service_principal=ServicePrincipal.sns
            )
            endpoint = subscriber["Endpoint"]
            if endpoint:
                delivery_stream = extract_resource_from_arn(endpoint).split("/")[1]
                firehose_client.put_record(
                    DeliveryStreamName=delivery_stream, Record={"Data": to_bytes(message_body)}
                )
                store_delivery_log(
                    context.message,
                    subscriber,
                    success=True,
                    topic_attributes=context.topic_attributes,
                )
        except Exception as exc:
            LOG.info(
                "Received error on sending SNS message, putting to DLQ (if configured): %s", exc
            )
            # TODO: check delivery log
            # TODO check DLQ?


class SmsPhoneNumberPublisher(EndpointPublisher):
    """
    The SMS publisher is responsible for publishing the SNS message directly to a phone number.
    This is not directly implemented yet in LocalStack, we only save the message.
    """

    def _publish(self, context: SnsPublishContext, endpoint: str):
        event = self.prepare_message(context.message, endpoint)
        context.store.sms_messages.append(event)
        LOG.info(
            "Delivering SMS message to %s: %s",
            event["PhoneNumber"],
            event["Message"],
        )

        # TODO: check about delivery logs for individual call, need a real AWS test
        # hard to know the format

    def prepare_message(self, message_context: SnsMessage, endpoint: str) -> dict:
        return {
            "PhoneNumber": endpoint,
            "TopicArn": None,
            "SubscriptionArn": None,
            "MessageId": message_context.message_id,
            "Message": message_context.message_content(protocol="sms"),
            "MessageAttributes": message_context.message_attributes,
            "MessageStructure": message_context.message_structure,
            "Subject": message_context.subject,
        }


class ApplicationEndpointPublisher(EndpointPublisher):
    """
    The application publisher is responsible for publishing the SNS message directly to a registered SNS application
    endpoint, without it being subscribed to a topic.
    See `ApplicationTopicPublisher` for more information about Application Endpoint publishing.
    """

    def _publish(self, context: SnsPublishContext, endpoint: str):
        message = self.prepare_message(context.message, endpoint)
        cache = context.store.platform_endpoint_messages.setdefault(endpoint, [])
        cache.append(message)

        if (
            config.LEGACY_SNS_GCM_PUBLISHING
            and get_platform_type_from_endpoint_arn(endpoint) == "GCM"
        ):
            self._legacy_publish_to_gcm(context, endpoint)

        # TODO: rewrite the platform application publishing logic
        #  will need to validate credentials when creating platform app earlier, need thorough testing

        # TODO: see about delivery log for individual endpoint message, need credentials for testing
        # store_delivery_log(subscriber, context, success=True)

    def prepare_message(self, message_context: SnsMessage, endpoint: str) -> Union[str, Dict]:
        platform_type = get_platform_type_from_endpoint_arn(endpoint)
        return {
            "TargetArn": endpoint,
            "TopicArn": "",
            "SubscriptionArn": "",
            "Message": message_context.message_content(protocol=platform_type),
            "MessageAttributes": message_context.message_attributes,
            "MessageStructure": message_context.message_structure,
            "Subject": message_context.subject,
            "MessageId": message_context.message_id,
        }

    @staticmethod
    def _legacy_publish_to_gcm(context: SnsPublishContext, endpoint: str):
        application_attributes, endpoint_attributes = get_attributes_for_application_endpoint(
            endpoint
        )
        send_message_to_gcm(
            context=context,
            app_attributes=application_attributes,
            endpoint_attributes=endpoint_attributes,
        )


def get_platform_type_from_endpoint_arn(endpoint_arn: str) -> SnsApplicationPlatforms:
    return endpoint_arn.rsplit("/", maxsplit=3)[1]  # noqa


def get_application_platform_arn_from_endpoint_arn(endpoint_arn: str) -> str:
    """
    Retrieve the application_platform information from the endpoint_arn to build the application platform ARN
    The format of the endpoint is:
    `arn:aws:sns:{region}:{account_id}:endpoint/{platform_type}/{application_name}/{endpoint_id}`
    :param endpoint_arn: str
    :return: application_platform_arn: str
    """
    parsed_arn = parse_arn(endpoint_arn)

    _, platform_type, app_name, _ = parsed_arn["resource"].split("/")
    base_arn = f"arn:aws:sns:{parsed_arn['region']}:{parsed_arn['account']}"
    return f"{base_arn}:app/{platform_type}/{app_name}"


def get_attributes_for_application_endpoint(endpoint_arn: str) -> Tuple[Dict, Dict]:
    """
    Retrieve the attributes necessary to send a message directly to the platform (credentials and token)
    :param endpoint_arn:
    :return:
    """
    account_id = extract_account_id_from_arn(endpoint_arn)
    region_name = extract_region_from_arn(endpoint_arn)

    sns_client = connect_to(aws_access_key_id=account_id, region_name=region_name).sns

    # TODO: we should access this from the moto store directly
    endpoint_attributes = sns_client.get_endpoint_attributes(EndpointArn=endpoint_arn)

    app_platform_arn = get_application_platform_arn_from_endpoint_arn(endpoint_arn)
    app = sns_client.get_platform_application_attributes(PlatformApplicationArn=app_platform_arn)

    return app.get("Attributes", {}), endpoint_attributes.get("Attributes", {})


def send_message_to_gcm(
    context: SnsPublishContext, app_attributes: Dict[str, str], endpoint_attributes: Dict[str, str]
) -> None:
    """
    Send the message directly to GCM, with the credentials used when creating the PlatformApplication and the Endpoint
    :param context: SnsPublishContext
    :param app_attributes: ApplicationPlatform attributes, contains PlatformCredential for GCM
    :param endpoint_attributes: Endpoint attributes, contains Token that represent the mobile endpoint
    :return:
    """
    server_key = app_attributes.get("PlatformCredential", "")
    token = endpoint_attributes.get("Token", "")
    # message is supposed to be a JSON string to GCM
    json_message = context.message.message_content("GCM")
    data = json.loads(json_message)

    data["to"] = token
    headers = {"Authorization": f"key={server_key}", "Content-type": "application/json"}

    response = requests.post(
        sns_constants.GCM_URL,
        headers=headers,
        data=json.dumps(data),
    )
    if response.status_code != 200:
        LOG.warning(
            "Platform GCM returned response %s with content %s",
            response.status_code,
            response.content,
        )


def compute_canonical_string(message: dict, notification_type: str) -> str:
    """
    The notification message signature is computed using the SHA1withRSA algorithm on a "canonical string" – a UTF-8
    string which observes certain conventions including the sort order of included fields. (Please note that any
    deviation in the construction of the message string described below such as excluding a field, including an extra
    space or changing sort order will result in a different validation signature which will not match the pre-computed
    message signature.)
    See https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
    """
    # create the canonical string
    if notification_type == SnsMessageType.Notification:
        fields = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    elif notification_type in (
        SnsMessageType.SubscriptionConfirmation,
        SnsMessageType.UnsubscribeConfirmation,
    ):
        fields = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
    else:
        return ""

    # create the canonical string
    string_to_sign = "".join([f"{f}\n{message[f]}\n" for f in fields if f in message])
    return string_to_sign


def get_message_signature(canonical_string: str, signature_version: str) -> str:
    chosen_hash = hashes.SHA256() if signature_version == "2" else hashes.SHA1()
    message_signature = SNS_SERVER_PRIVATE_KEY.sign(
        to_bytes(canonical_string),
        padding=padding.PKCS1v15(),
        algorithm=chosen_hash,
    )
    # base64 encode the signature
    encoded_signature = base64.b64encode(message_signature)
    return to_str(encoded_signature)


def create_sns_message_body(
    message_context: SnsMessage,
    subscriber: SnsSubscription,
    topic_attributes: dict[str, str] = None,
) -> str:
    message_type = message_context.type or "Notification"
    protocol = subscriber["Protocol"]
    message_content = message_context.message_content(protocol)

    if message_type == "Notification" and is_raw_message_delivery(subscriber):
        return message_content

    external_url = get_cert_base_url()

    data = {
        "Type": message_type,
        "MessageId": message_context.message_id,
        "TopicArn": subscriber["TopicArn"],
        "Message": message_content,
        "Timestamp": timestamp_millis(),
    }

    if message_type == SnsMessageType.Notification:
        unsubscribe_url = create_unsubscribe_url(external_url, subscriber["SubscriptionArn"])
        data["UnsubscribeURL"] = unsubscribe_url

    elif message_type in (
        SnsMessageType.SubscriptionConfirmation,
        SnsMessageType.UnsubscribeConfirmation,
    ):
        data["Token"] = message_context.token
        data["SubscribeURL"] = create_subscribe_url(
            external_url, subscriber["TopicArn"], message_context.token
        )

    if message_context.subject:
        data["Subject"] = message_context.subject

    if message_context.message_attributes:
        data["MessageAttributes"] = prepare_message_attributes(message_context.message_attributes)

    # FIFO topics do not add the signature in the message
    if not subscriber.get("TopicArn", "").endswith(".fifo"):
        signature_version = (
            topic_attributes.get("signature_version", "1") if topic_attributes else "1"
        )
        canonical_string = compute_canonical_string(data, message_type)
        signature = get_message_signature(canonical_string, signature_version=signature_version)
        data.update(
            {
                "SignatureVersion": signature_version,
                "Signature": signature,
                "SigningCertURL": f"{external_url}{sns_constants.SNS_CERT_ENDPOINT}",
            }
        )
    else:
        data["SequenceNumber"] = message_context.sequencer_number

    return json.dumps(data)


def prepare_message_attributes(
    message_attributes: MessageAttributeMap,
) -> Dict[str, Dict[str, str]]:
    attributes = {}
    if not message_attributes:
        return attributes
    # TODO: Number type is not supported for Lambda subscriptions, passed as String
    #  do conversion here
    for attr_name, attr in message_attributes.items():
        data_type = attr["DataType"]
        if data_type.startswith("Binary"):
            # binary payload in base64 encoded by AWS, UTF-8 for JSON
            # https://docs.aws.amazon.com/sns/latest/api/API_MessageAttributeValue.html
            val = base64.b64encode(attr["BinaryValue"]).decode()
        else:
            val = attr.get("StringValue")

        attributes[attr_name] = {
            "Type": data_type,
            "Value": val,
        }
    return attributes


def is_raw_message_delivery(subscriber: SnsSubscription) -> bool:
    return subscriber.get("RawMessageDelivery") in ("true", True, "True")


def is_fifo_topic(subscriber: SnsSubscription) -> bool:
    return subscriber.get("TopicArn", "").endswith(".fifo")


def store_delivery_log(
    message_context: SnsMessage,
    subscriber: SnsSubscription,
    success: bool,
    topic_attributes: dict[str, str] = None,
    delivery: dict = None,
):
    """
    Store the delivery logs in CloudWatch, configured as TopicAttributes
    See: https://docs.aws.amazon.com/sns/latest/dg/sns-topic-attributes.html#msg-status-sdk

    TODO: for Application, you can also configure Platform attributes:
    See:https://docs.aws.amazon.com/sns/latest/dg/sns-msg-status.html
    """
    # TODO: effectively use `<ENDPOINT>SuccessFeedbackSampleRate` to sample delivery logs
    # TODO: validate format of `delivery` for each Publisher
    # map Protocol to TopicAttribute
    available_delivery_logs_services = {
        "http",
        "https",
        "firehose",
        "lambda",
        "application",
        "sqs",
    }
    # SMS is a special case: https://docs.aws.amazon.com/sns/latest/dg/sms_stats_cloudwatch.html
    # seems like you need to configure on the Console, leave it on by default now in LocalStack
    protocol = subscriber.get("Protocol")

    if protocol != "sms":
        if protocol not in available_delivery_logs_services or not topic_attributes:
            # this service does not have DeliveryLogs feature, return
            return

        # TODO: for now, those attributes are stored as attributes of the moto Topic model in snake case
        # see to work this in our store instead
        role_type = "success" if success else "failure"
        topic_attribute = f"{protocol}_{role_type}_feedback_role_arn"

        # check if topic has the right attribute and a role, otherwise return
        # TODO: on purpose not using walrus operator to show that we get the RoleArn here for CloudWatch
        role_arn = topic_attributes.get(topic_attribute)
        if not role_arn:
            return

    if not is_api_enabled("logs"):
        LOG.warning(
            "Service 'logs' is not enabled: skip storing SNS delivery logs. "
            "Please check your 'SERVICES' configuration variable."
        )
        return

    log_group_name = subscriber.get("TopicArn", "")
    for partition in PARTITION_NAMES:
        log_group_name = log_group_name.replace(f"arn:{partition}:", "")
    log_group_name = log_group_name.replace(":", "/")
    log_stream_name = long_uid()
    invocation_time = int(time.time() * 1000)

    delivery = not_none_or(delivery, {})
    delivery["deliveryId"] = long_uid()
    delivery["destination"] = subscriber.get("Endpoint", "")
    delivery["dwellTimeMs"] = 200
    if not success:
        delivery["attemps"] = 1

    if (protocol := subscriber["Protocol"]) == "application":
        protocol = get_platform_type_from_endpoint_arn(subscriber["Endpoint"])

    message = message_context.message_content(protocol)
    delivery_log = {
        "notification": {
            "messageMD5Sum": md5(message),
            "messageId": message_context.message_id,
            "topicArn": subscriber.get("TopicArn"),
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f%z"),
        },
        "delivery": delivery,
        "status": "SUCCESS" if success else "FAILURE",
    }

    log_output = json.dumps(delivery_log)

    # TODO: use the account/region from the role in the TopicAttribute instead, this is what AWS uses
    account_id = extract_account_id_from_arn(subscriber["TopicArn"])
    region_name = extract_region_from_arn(subscriber["TopicArn"])
    logs_client = connect_to(aws_access_key_id=account_id, region_name=region_name).logs

    return store_cloudwatch_logs(
        logs_client, log_group_name, log_stream_name, log_output, invocation_time
    )


def get_cert_base_url() -> str:
    if config.SNS_CERT_URL_HOST:
        return f"https://{config.SNS_CERT_URL_HOST}"

    return external_service_url().rstrip("/")


def create_subscribe_url(external_url, topic_arn, subscription_token):
    return f"{external_url}/?Action=ConfirmSubscription&TopicArn={topic_arn}&Token={subscription_token}"


def create_unsubscribe_url(external_url, subscription_arn):
    return f"{external_url}/?Action=Unsubscribe&SubscriptionArn={subscription_arn}"


class PublishDispatcher:
    """
    The PublishDispatcher is responsible for dispatching the publishing of SNS messages asynchronously to worker
    threads via a `ThreadPoolExecutor`, depending on the SNS subscriber protocol and filter policy.
    """

    topic_notifiers = {
        "http": HttpTopicPublisher(),
        "https": HttpTopicPublisher(),
        "email": EmailTopicPublisher(),
        "email-json": EmailJsonTopicPublisher(),
        "sms": SmsTopicPublisher(),
        "sqs": SqsTopicPublisher(),
        "application": ApplicationTopicPublisher(),
        "lambda": LambdaTopicPublisher(),
        "firehose": FirehoseTopicPublisher(),
    }
    batch_topic_notifiers = {"sqs": SqsBatchTopicPublisher()}
    sms_notifier = SmsPhoneNumberPublisher()
    application_notifier = ApplicationEndpointPublisher()

    subscription_filter = SubscriptionFilter()

    def __init__(self, num_thread: int = 10):
        self.executor = ThreadPoolExecutor(num_thread, thread_name_prefix="sns_pub")
        self.topic_partitioned_executor = TopicPartitionedThreadPoolExecutor(
            max_workers=num_thread, thread_name_prefix="sns_pub_fifo"
        )

    def shutdown(self):
        self.executor.shutdown(wait=False)
        self.topic_partitioned_executor.shutdown(wait=False)

    def _should_publish(
        self,
        subscription_filter_policy: dict[str, dict],
        message_ctx: SnsMessage,
        subscriber: SnsSubscription,
    ):
        """
        Validate that the message should be relayed to the subscriber, depending on the filter policy and the
        subscription status
        """
        # FIXME: for now, send to email even if not confirmed, as we do not send the token to confirm to email
        # subscriptions
        if (
            not subscriber["PendingConfirmation"] == "false"
            and "email" not in subscriber["Protocol"]
        ):
            return

        subscriber_arn = subscriber["SubscriptionArn"]
        filter_policy = subscription_filter_policy.get(subscriber_arn)
        if not filter_policy:
            return True
        # default value is `MessageAttributes`
        match subscriber.get("FilterPolicyScope", "MessageAttributes"):
            case "MessageAttributes":
                return self.subscription_filter.check_filter_policy_on_message_attributes(
                    filter_policy=filter_policy, message_attributes=message_ctx.message_attributes
                )
            case "MessageBody":
                return self.subscription_filter.check_filter_policy_on_message_body(
                    filter_policy=filter_policy,
                    message_body=message_ctx.message_content(subscriber["Protocol"]),
                )

    def publish_to_topic(self, ctx: SnsPublishContext, topic_arn: str) -> None:
        subscriptions = ctx.store.get_topic_subscriptions(topic_arn)
        for subscriber in subscriptions:
            if self._should_publish(ctx.store.subscription_filter_policy, ctx.message, subscriber):
                notifier = self.topic_notifiers[subscriber["Protocol"]]
                LOG.debug(
                    "Topic '%s' publishing '%s' to subscribed '%s' with protocol '%s' (subscription '%s')",
                    topic_arn,
                    ctx.message.message_id,
                    subscriber.get("Endpoint"),
                    subscriber["Protocol"],
                    subscriber["SubscriptionArn"],
                )
                self._submit_notification(notifier, ctx, subscriber)

    def publish_batch_to_topic(self, ctx: SnsBatchPublishContext, topic_arn: str) -> None:
        subscriptions = ctx.store.get_topic_subscriptions(topic_arn)
        for subscriber in subscriptions:
            protocol = subscriber["Protocol"]
            notifier = self.batch_topic_notifiers.get(protocol)
            # does the notifier supports batching natively? for now, only SQS supports it
            if notifier:
                subscriber_ctx = ctx
                messages_amount_before_filtering = len(ctx.messages)
                filtered_messages = [
                    message
                    for message in ctx.messages
                    if self._should_publish(
                        ctx.store.subscription_filter_policy, message, subscriber
                    )
                ]
                if not filtered_messages:
                    LOG.debug(
                        "No messages match filter policy, not publishing batch from topic '%s' to subscription '%s'",
                        topic_arn,
                        subscriber["SubscriptionArn"],
                    )
                    continue

                messages_amount = len(filtered_messages)
                if messages_amount != messages_amount_before_filtering:
                    LOG.debug(
                        "After applying subscription filter, %s out of %s message(s) to be sent to '%s'",
                        messages_amount,
                        messages_amount_before_filtering,
                        subscriber["SubscriptionArn"],
                    )
                    # We need to copy the context to not overwrite the messages after filtering messages, otherwise we
                    # would filter on the same context for different subscribers
                    subscriber_ctx = copy.copy(ctx)
                    subscriber_ctx.messages = filtered_messages

                LOG.debug(
                    "Topic '%s' batch publishing %s messages to subscribed '%s' with protocol '%s' (subscription '%s')",
                    topic_arn,
                    messages_amount,
                    subscriber.get("Endpoint"),
                    subscriber["Protocol"],
                    subscriber["SubscriptionArn"],
                )
                self._submit_notification(notifier, subscriber_ctx, subscriber)
            else:
                # if no batch support, fall back to sending them sequentially
                notifier = self.topic_notifiers[subscriber["Protocol"]]
                for message in ctx.messages:
                    if self._should_publish(
                        ctx.store.subscription_filter_policy, message, subscriber
                    ):
                        individual_ctx = SnsPublishContext(
                            message=message, store=ctx.store, request_headers=ctx.request_headers
                        )
                        LOG.debug(
                            "Topic '%s' batch publishing '%s' to subscribed '%s' with protocol '%s' (subscription '%s')",
                            topic_arn,
                            individual_ctx.message.message_id,
                            subscriber.get("Endpoint"),
                            subscriber["Protocol"],
                            subscriber["SubscriptionArn"],
                        )
                        self._submit_notification(notifier, individual_ctx, subscriber)

    def _submit_notification(
        self, notifier, ctx: SnsPublishContext | SnsBatchPublishContext, subscriber: SnsSubscription
    ):
        if (topic_arn := subscriber.get("TopicArn", "")).endswith(".fifo"):
            # TODO: we still need to implement Message deduplication on the topic level with `should_publish` for FIFO
            self.topic_partitioned_executor.submit(
                notifier.publish, topic_arn, context=ctx, subscriber=subscriber
            )
        else:
            self.executor.submit(notifier.publish, context=ctx, subscriber=subscriber)

    def publish_to_phone_number(self, ctx: SnsPublishContext, phone_number: str) -> None:
        LOG.debug(
            "Publishing '%s' to phone number '%s' with protocol 'sms'",
            ctx.message.message_id,
            phone_number,
        )
        self.executor.submit(self.sms_notifier.publish, context=ctx, endpoint=phone_number)

    def publish_to_application_endpoint(self, ctx: SnsPublishContext, endpoint_arn: str) -> None:
        LOG.debug(
            "Publishing '%s' to application endpoint '%s'",
            ctx.message.message_id,
            endpoint_arn,
        )
        self.executor.submit(self.application_notifier.publish, context=ctx, endpoint=endpoint_arn)

    def publish_to_topic_subscriber(
        self, ctx: SnsPublishContext, topic_arn: str, subscription_arn: str
    ) -> None:
        """
        This allows us to publish specific HTTP(S) messages specific to those endpoints, namely
        `SubscriptionConfirmation` and `UnsubscribeConfirmation`. Those are "topic" messages in shape, but are sent
        only to the endpoint subscribing or unsubscribing.
        This is only used internally.
        Note: might be needed for multi account SQS and Lambda `SubscriptionConfirmation`
        :param ctx: SnsPublishContext
        :param topic_arn: the topic of the subscriber
        :param subscription_arn: the ARN of the subscriber
        :return: None
        """
        subscriber = ctx.store.subscriptions.get(subscription_arn)
        if not subscriber:
            return
        notifier = self.topic_notifiers[subscriber["Protocol"]]
        LOG.debug(
            "Topic '%s' publishing '%s' to subscribed '%s' with protocol '%s' (Id='%s', Subscription='%s')",
            topic_arn,
            ctx.message.type,
            subscription_arn,
            subscriber["Protocol"],
            ctx.message.message_id,
            subscriber.get("Endpoint"),
        )
        self.executor.submit(notifier.publish, context=ctx, subscriber=subscriber)
