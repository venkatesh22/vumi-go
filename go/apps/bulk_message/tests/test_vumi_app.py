# -*- coding: utf-8 -*-

"""Tests for go.vumitools.bulk_send_application"""

from twisted.internet.defer import inlineCallbacks
from twisted.internet.task import Clock

from vumi.message import TransportUserMessage

from go.vumitools.tests.utils import AppWorkerTestCase
from go.vumitools.window_manager import WindowManager
from go.apps.bulk_message.vumi_app import BulkMessageApplication


class TestBulkMessageApplication(AppWorkerTestCase):

    application_class = BulkMessageApplication

    @inlineCallbacks
    def setUp(self):
        super(TestBulkMessageApplication, self).setUp()

        # Patch the clock so we can control time
        self.clock = Clock()
        self.patch(WindowManager, 'get_clock', lambda _: self.clock)

        self.config = self.mk_config({})
        self.app = yield self.get_application(self.config)

        # Steal app's vumi_api
        self.vumi_api = self.app.vumi_api  # YOINK!

        # Create a test user account
        self.user_account = yield self.mk_user(self.vumi_api, u'testuser')
        self.user_api = self.vumi_api.get_user_api(self.user_account.key)

        yield self.user_api.api.declare_tags([("pool", "tag1"),
                                              ("pool", "tag2")])
        yield self.user_api.api.set_pool_metadata("pool", {
            "transport_type": "sphex",
            })

    def store_outbound(self, **kw):
        return self.vumi_api.mdb.add_outbound_message(self.mkmsg_out(**kw))

    @inlineCallbacks
    def test_start(self):
        user_api = self.user_api
        group = yield user_api.contact_store.new_group(u'test group')
        contact1 = yield user_api.contact_store.new_contact(
            name=u'First', surname=u'Contact', msisdn=u'27831234567',
            groups=[group])
        contact2 = yield user_api.contact_store.new_contact(
            name=u'Second', surname=u'Contact', msisdn=u'27831234568',
            groups=[group])
        conversation = yield self.create_conversation(
            delivery_tag_pool=u'pool', delivery_class=u'sms')
        conversation.add_group(group)
        yield conversation.save()

        yield self.start_conversation(conversation)

        # batch_id
        [batch_id] = conversation.batches.keys()

        # check commands made it through to the dispatcher and the vumi_app
        [disp_cmd] = self.get_dispatcher_commands()
        self.assertEqual(disp_cmd['command'], 'start')
        [bulk_cmd] = self.get_app_message_commands()
        self.assertEqual(bulk_cmd['command'], 'start')

        # Force processing of messages
        yield self._amqp.kick_delivery()

        # Assert that the messages are in the window managers' flight window
        window_id = self.app.get_window_id(conversation.key, batch_id)
        self.assertEqual(
            (yield self.app.window_manager.count_waiting(window_id)), 2)

        # Go past the monitoring interval to ensure the window is
        # being worked through for delivery
        self.clock.advance(self.app.monitor_interval + 1)

        # assert that we've sent the message to the two contacts
        msgs = yield self.wait_for_dispatched_messages(2)
        msgs.sort(key=lambda msg: msg['to_addr'])
        [msg1, msg2] = msgs

        # Create an ack and a nack for the messages
        ack = self.mkmsg_ack(user_message_id=msg1['message_id'],
            sent_message_id=msg1['message_id'])
        nack = self.mkmsg_nack(user_message_id=msg2['message_id'],
            nack_reason='unknown')

        yield self.dispatch_event(ack)
        yield self.dispatch_event(nack)

        # Assert that the window's now empty because acks have been received
        self.assertEqual(
            (yield self.app.window_manager.count_in_flight(window_id)), 0)
        self.assertEqual(
            (yield self.app.window_manager.count_waiting(window_id)), 0)

        # check that the right to_addr & from_addr are set and that the content
        # of the message equals conversation.message
        self.assertEqual(msg1['to_addr'], contact1.msisdn)
        self.assertEqual(msg2['to_addr'], contact2.msisdn)

        # check tags and user accounts
        for msg in msgs:
            tag = msg['helper_metadata']['tag']['tag']
            user_account_key = msg['helper_metadata']['go']['user_account']
            self.assertEqual(tag, ["pool", "tag1"])
            self.assertEqual(user_account_key, self.user_account.key)

        batch_status = yield self.vumi_api.mdb.batch_status(batch_id)
        self.assertEqual(batch_status['sent'], 2)
        dbmsgs = yield self.vumi_api.mdb.batch_outbound_keys(batch_id)
        self.assertEqual(sorted([msg1['message_id'], msg2['message_id']]),
                         sorted(dbmsgs))

    @inlineCallbacks
    def test_start_with_deduplication(self):
        user_api = self.user_api
        group = yield user_api.contact_store.new_group(u'test group')

        # Create two contacts with the same to_addr, they should be deduped

        contact1 = yield user_api.contact_store.new_contact(
            name=u'First', surname=u'Contact', msisdn=u'27831234567',
            groups=[group])
        contact2 = yield user_api.contact_store.new_contact(
            name=u'Second', surname=u'Contact', msisdn=u'27831234567',
            groups=[group])
        conversation = yield self.create_conversation(
            delivery_tag_pool=u'pool', delivery_class=u'sms')
        conversation.add_group(group)
        yield conversation.save()

        # Provide the dedupe option to the conversation
        yield self.start_conversation(conversation, dedupe=True)

        yield self._amqp.kick_delivery()

        # Go past the monitoring interval to ensure the window is
        # being worked through for delivery
        self.clock.advance(self.app.monitor_interval + 1)

        yield self._amqp.kick_delivery()

        # Make sure only 1 message is sent, the rest were duplicates to the
        # same to_addr and were filtered out as a result.
        [msg] = self.get_dispatched_messages()

        # check that the right to_addr & from_addr are set and that the content
        # of the message equals conversation.message
        self.assertEqual(msg['to_addr'], contact1.msisdn)
        self.assertEqual(msg['to_addr'], contact2.msisdn)

    @inlineCallbacks
    def test_consume_ack(self):
        yield self.store_outbound(message_id='123')
        ack_event = yield self.publish_event(user_message_id='123',
                                             event_type='ack',
                                             sent_message_id='xyz')
        [event_id] = yield self.vumi_api.mdb.message_event_keys('123')
        self.assertEqual(event_id, ack_event['event_id'])

    @inlineCallbacks
    def test_consume_delivery_report(self):
        yield self.store_outbound(message_id='123')
        dr_event = yield self.publish_event(user_message_id='123',
                                            event_type='delivery_report',
                                            delivery_status='delivered')
        [event_id] = yield self.vumi_api.mdb.message_event_keys('123')
        self.assertEqual(event_id, dr_event['event_id'])

    @inlineCallbacks
    def test_consume_user_message(self):
        msg = self.mkmsg_in()
        yield self.dispatch(msg)
        dbmsg = yield self.vumi_api.mdb.get_inbound_message(msg['message_id'])
        self.assertEqual(dbmsg, msg)

    @inlineCallbacks
    def test_close_session(self):
        msg = self.mkmsg_in(session_event=TransportUserMessage.SESSION_CLOSE)
        yield self.dispatch(msg)
        dbmsg = yield self.vumi_api.mdb.get_inbound_message(msg['message_id'])
        self.assertEqual(dbmsg, msg)

    @inlineCallbacks
    def test_send_message_command(self):
        user_account_key = "4f5gfdtrfe44rgffserf"
        msg_options = {
            'transport_name': 'sphex_transport',
            'from_addr': '666666',
            'transport_type': 'sphex',
            "helper_metadata": {
                "go": {
                    "user_account": user_account_key
                },
                'tag': {
                    'tag': ['pool', 'tag1']
                },
            }
        }
        yield self.dispatch_command("send_message", command_data={
                    "batch_id": "345dt54fgtffdsft54ffg",
                    "to_addr": "123456",
                    "content": "hello world",
                    "msg_options": msg_options
                    })

        [msg] = yield self.get_dispatched_messages()
        self.assertEqual(msg.payload['to_addr'], "123456")
        self.assertEqual(msg.payload['from_addr'], "666666")
        self.assertEqual(msg.payload['content'], "hello world")
        self.assertEqual(msg.payload['transport_name'], "sphex_transport")
        self.assertEqual(msg.payload['transport_type'], "sphex")
        self.assertEqual(msg.payload['message_type'], "user_message")
        self.assertEqual(msg.payload['helper_metadata']['go']['user_account'],
                                                            user_account_key)
        self.assertEqual(msg.payload['helper_metadata']['tag']['tag'],
                                                            ['pool', 'tag1'])

    @inlineCallbacks
    def test_collect_metrics(self):
        conv = yield self.create_conversation(
            delivery_tag_pool=u'pool', delivery_class=u'sms')
        yield self.start_conversation(conv)
        [batch_id] = conv.get_batch_keys()

        mkid = TransportUserMessage.generate_id
        yield self.user_api.api.mdb.add_outbound_message(
            self.mkmsg_out("out 1", message_id=mkid()), batch_id=batch_id)
        yield self.user_api.api.mdb.add_outbound_message(
            self.mkmsg_out("out 2", message_id=mkid()), batch_id=batch_id)
        yield self.user_api.api.mdb.add_inbound_message(
            self.mkmsg_in("in 2", message_id=mkid()), batch_id=batch_id)

        yield self.dispatch_command(
            'collect_metrics', conversation_key=conv.key,
            user_account_key=self.user_account.key)
        metrics = self.poll_metrics('%s.%s' % (self.user_account.key,
                                               conv.key))
        self.assertEqual({
                u'messages_sent': [2],
                u'messages_received': [1],
                }, metrics)
