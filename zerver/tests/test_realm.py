import json
import os
import random
import re
import string
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from unittest import mock, skipUnless

import orjson
import time_machine
from django.conf import settings
from django.core import mail
from django.test import override_settings
from django.utils.timezone import now as timezone_now
from typing_extensions import override

from confirmation.models import Confirmation, create_confirmation_link
from zerver.actions.create_realm import do_change_realm_subdomain, do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.message_send import (
    internal_send_group_direct_message,
    internal_send_private_message,
    internal_send_stream_message,
)
from zerver.actions.realm_settings import (
    clean_deactivated_realm_data,
    delete_expired_demo_organizations,
    do_add_deactivated_redirect,
    do_change_realm_max_invites,
    do_change_realm_org_type,
    do_change_realm_permission_group_setting,
    do_change_realm_plan_type,
    do_deactivate_realm,
    do_delete_all_realm_attachments,
    do_reactivate_realm,
    do_scrub_realm,
    do_send_realm_reactivation_email,
    do_set_realm_authentication_methods,
    do_set_realm_property,
    do_set_realm_user_default_setting,
)
from zerver.actions.streams import do_deactivate_stream, merge_streams
from zerver.actions.user_groups import check_add_user_group
from zerver.lib.realm_description import get_realm_rendered_description, get_realm_text_description
from zerver.lib.send_email import send_future_email
from zerver.lib.streams import create_stream_if_needed
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import activate_push_notification_service
from zerver.lib.upload import delete_message_attachments, upload_message_attachment
from zerver.models import (
    Attachment,
    CustomProfileField,
    Message,
    NamedUserGroup,
    Realm,
    RealmAuditLog,
    RealmReactivationStatus,
    RealmUserDefault,
    ScheduledEmail,
    Stream,
    UserGroupMembership,
    UserMessage,
    UserProfile,
)
from zerver.models.groups import SystemGroups
from zerver.models.realm_audit_logs import AuditLogEventType
from zerver.models.realms import get_realm
from zerver.models.streams import get_stream
from zerver.models.users import get_system_bot, get_user_profile_by_id

if settings.ZILENCER_ENABLED:
    from corporate.lib.stripe import get_seat_count


class RealmTest(ZulipTestCase):
    def assert_user_profile_cache_gets_new_name(
        self, user_profile: UserProfile, new_realm_name: str
    ) -> None:
        self.assertEqual(user_profile.realm.name, new_realm_name)

    def test_realm_creation_ensures_internal_realms(self) -> None:
        with mock.patch("zerver.actions.create_realm.server_initialized", return_value=False):
            with (
                mock.patch(
                    "zerver.actions.create_realm.create_internal_realm"
                ) as mock_create_internal,
                self.assertLogs(level="INFO") as info_logs,
            ):
                do_create_realm("testrealm", "Test Realm")
                mock_create_internal.assert_called_once()
            self.assertEqual(
                info_logs.output,
                ["INFO:root:Server not yet initialized. Creating the internal realm first."],
            )

    def test_realm_creation_on_special_subdomains_disallowed(self) -> None:
        with self.settings(SOCIAL_AUTH_SUBDOMAIN="zulipauth"), self.assertRaises(AssertionError):
            do_create_realm("zulipauth", "Test Realm")

        with (
            self.settings(SELF_HOSTING_MANAGEMENT_SUBDOMAIN="zulipselfhosting"),
            self.assertRaises(AssertionError),
        ):
            do_create_realm("zulipselfhosting", "Test Realm")

    def test_permission_for_education_non_profit_organization(self) -> None:
        realm = do_create_realm(
            "test_education_non_profit",
            "education_org_name",
            org_type=Realm.ORG_TYPES["education_nonprofit"]["id"],
        )

        admins_group = NamedUserGroup.objects.get(
            name=SystemGroups.ADMINISTRATORS, realm=realm, is_system_group=True
        )
        self.assertEqual(realm.can_create_public_channel_group_id, admins_group.id)

        realm = get_realm("test_education_non_profit")
        moderators_group = NamedUserGroup.objects.get(
            name=SystemGroups.MODERATORS, realm=realm, is_system_group=True
        )
        self.assertEqual(realm.can_add_subscribers_group.id, moderators_group.id)
        self.assertEqual(realm.can_create_groups.id, moderators_group.id)
        self.assertEqual(realm.can_invite_users_group.id, admins_group.id)
        self.assertEqual(realm.can_move_messages_between_channels_group.id, moderators_group.id)

    def test_permission_for_education_for_profit_organization(self) -> None:
        realm = do_create_realm(
            "test_education_for_profit",
            "education_org_name",
            org_type=Realm.ORG_TYPES["education"]["id"],
        )

        admins_group = NamedUserGroup.objects.get(
            name=SystemGroups.ADMINISTRATORS, realm=realm, is_system_group=True
        )
        self.assertEqual(realm.can_create_public_channel_group_id, admins_group.id)

        realm = get_realm("test_education_for_profit")
        moderators_group = NamedUserGroup.objects.get(
            name=SystemGroups.MODERATORS, realm=realm, is_system_group=True
        )
        self.assertEqual(realm.can_add_subscribers_group.id, moderators_group.id)
        self.assertEqual(realm.can_create_groups.id, moderators_group.id)
        self.assertEqual(realm.can_invite_users_group.id, admins_group.id)
        self.assertEqual(realm.can_move_messages_between_channels_group.id, moderators_group.id)

    def test_realm_enable_spectator_access(self) -> None:
        realm = do_create_realm(
            "test_web_public_true",
            "Foo",
            plan_type=Realm.PLAN_TYPE_STANDARD,
            enable_spectator_access=True,
        )
        self.assertEqual(realm.enable_spectator_access, True)

        realm = do_create_realm("test_web_public_false", "Boo", enable_spectator_access=False)
        self.assertEqual(realm.enable_spectator_access, False)

        with self.assertRaises(AssertionError):
            realm = do_create_realm("test_web_public_false_1", "Foo", enable_spectator_access=True)

        with self.assertRaises(AssertionError):
            realm = do_create_realm(
                "test_web_public_false_2",
                "Foo",
                plan_type=Realm.PLAN_TYPE_LIMITED,
                enable_spectator_access=True,
            )

    def test_do_set_realm_name_caching(self) -> None:
        """The main complicated thing about setting realm names is fighting the
        cache, and we start by populating the cache for Hamlet, and we end
        by checking the cache to ensure that the new value is there."""
        realm = get_realm("zulip")
        new_name = "Zed You Elle Eye Pea"
        do_set_realm_property(realm, "name", new_name, acting_user=None)
        self.assertEqual(get_realm(realm.string_id).name, new_name)
        self.assert_user_profile_cache_gets_new_name(self.example_user("hamlet"), new_name)

    def test_update_realm_name_events(self) -> None:
        realm = get_realm("zulip")
        new_name = "Puliz"
        with self.capture_send_event_calls(expected_num_events=1) as events:
            do_set_realm_property(realm, "name", new_name, acting_user=None)
        event = events[0]["event"]
        self.assertEqual(
            event,
            dict(
                type="realm",
                op="update",
                property="name",
                value=new_name,
            ),
        )

    def test_update_realm_description_events(self) -> None:
        realm = get_realm("zulip")
        new_description = "zulip dev group"
        with self.capture_send_event_calls(expected_num_events=1) as events:
            do_set_realm_property(realm, "description", new_description, acting_user=None)
        event = events[0]["event"]
        self.assertEqual(
            event,
            dict(
                type="realm",
                op="update",
                property="description",
                value=new_description,
            ),
        )

    def test_update_realm_description(self) -> None:
        self.login("iago")
        new_description = "zulip dev group"
        data = dict(description=new_description)
        with self.capture_send_event_calls(expected_num_events=1) as events:
            result = self.client_patch("/json/realm", data)
            self.assert_json_success(result)
            realm = get_realm("zulip")
            self.assertEqual(realm.description, new_description)

        event = events[0]["event"]
        self.assertEqual(
            event,
            dict(
                type="realm",
                op="update",
                property="description",
                value=new_description,
            ),
        )

    def test_realm_description_length(self) -> None:
        new_description = "A" * 1001
        data = dict(description=new_description)

        # create an admin user
        self.login("iago")

        result = self.client_patch("/json/realm", data)
        self.assert_json_error(result, "description is too long (limit: 1000 characters)")
        realm = get_realm("zulip")
        self.assertNotEqual(realm.description, new_description)

    def test_demo_organization_invite_required(self) -> None:
        realm = get_realm("zulip")
        self.assertFalse(realm.invite_required)

        self.login("desdemona")
        data = dict(invite_required="true")
        result = self.client_patch("/json/realm", data)
        self.assert_json_success(result)
        realm.refresh_from_db()
        self.assertTrue(realm.invite_required)

        # Update realm to be a demo organization
        realm.demo_organization_scheduled_deletion_date = timezone_now() + timedelta(days=30)
        realm.save()

        # Demo organization owner's don't have an email address set initially
        desdemona = self.example_user("desdemona")
        desdemona.delivery_email = ""
        desdemona.save()

        data = dict(invite_required="false")
        result = self.client_patch("/json/realm", data)
        self.assert_json_error(result, "Configure owner account email address.")

        desdemona.delivery_email = "desdemona@zulip.com"
        desdemona.save()
        result = self.client_patch("/json/realm", data)
        self.assert_json_success(result)
        realm.refresh_from_db()
        self.assertFalse(realm.invite_required)

    def test_realm_convert_demo_realm(self) -> None:
        data = dict(string_id="coolrealm")

        self.login("iago")
        result = self.client_patch("/json/realm", data)
        self.assert_json_error(result, "Must be an organization owner")

        self.login("desdemona")
        result = self.client_patch("/json/realm", data)
        self.assert_json_error(result, "Must be a demo organization.")

        realm = get_realm("zulip")
        realm.demo_organization_scheduled_deletion_date = timezone_now() + timedelta(days=30)
        realm.save()

        # Demo organization owner must have added an email before converting.
        desdemona = self.example_user("desdemona")
        desdemona.delivery_email = ""
        desdemona.save()
        result = self.client_patch("/json/realm", data)
        self.assert_json_error(result, "Configure owner account email address.")

        desdemona.delivery_email = "desdemona@zulip.com"
        desdemona.save()

        # Subdomain must be available to convert demo organization.
        data = dict(string_id="lear")
        result = self.client_patch("/json/realm", data)
        self.assert_json_error(
            result, "Subdomain is already in use. Please choose a different one."
        )

        # Now try to change the string_id to something available.
        data = dict(string_id="coolrealm")
        result = self.client_patch("/json/realm", data)
        self.assert_json_success(result)
        json = orjson.loads(result.content)
        self.assertEqual(json["realm_uri"], "http://coolrealm.testserver")
        self.assertEqual(json["realm_url"], "http://coolrealm.testserver")
        realm = get_realm("coolrealm")
        self.assertIsNone(realm.demo_organization_scheduled_deletion_date)
        self.assertEqual(realm.string_id, data["string_id"])

    def test_realm_name_length(self) -> None:
        new_name = "A" * (Realm.MAX_REALM_NAME_LENGTH + 1)
        data = dict(name=new_name)

        # create an admin user
        self.login("iago")

        result = self.client_patch("/json/realm", data)
        self.assert_json_error(result, "name is too long (limit: 40 characters)")
        realm = get_realm("zulip")
        self.assertNotEqual(realm.name, new_name)

    def test_admin_restrictions_for_changing_realm_name(self) -> None:
        new_name = "Mice will play while the cat is away"

        self.login("othello")

        req = dict(name=new_name)
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Must be an organization administrator")

    def test_unauthorized_name_change(self) -> None:
        data = {"full_name": "Sir Hamlet"}
        user_profile = self.example_user("hamlet")
        self.login_user(user_profile)
        do_set_realm_property(user_profile.realm, "name_changes_disabled", True, acting_user=None)
        url = "/json/settings"
        result = self.client_patch(url, data)
        self.assertEqual(result.status_code, 200)
        # Since the setting fails silently, no message is returned
        self.assert_in_response("", result)
        # Realm admins can change their name even setting is disabled.
        data = {"full_name": "New Iago"}
        self.login("iago")
        url = "/json/settings"
        result = self.client_patch(url, data)
        self.assert_json_success(result)

    def test_do_deactivate_realm_clears_user_realm_cache(self) -> None:
        """The main complicated thing about deactivating realm names is
        updating the cache, and we start by populating the cache for
        Hamlet, and we end by checking the cache to ensure that his
        realm appears to be deactivated.  You can make this test fail
        by disabling cache.flush_realm()."""
        hamlet_id = self.example_user("hamlet").id
        get_user_profile_by_id(hamlet_id)
        realm = get_realm("zulip")
        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        user = get_user_profile_by_id(hamlet_id)
        self.assertTrue(user.realm.deactivated)

    def test_do_change_realm_delete_clears_user_realm_cache(self) -> None:
        hamlet_id = self.example_user("hamlet").id
        get_user_profile_by_id(hamlet_id)
        realm = get_realm("zulip")
        realm.delete()
        with self.assertRaises(UserProfile.DoesNotExist):
            get_user_profile_by_id(hamlet_id)

    def test_do_change_realm_subdomain_clears_user_realm_cache(self) -> None:
        """The main complicated thing about changing realm subdomains is
        updating the cache, and we start by populating the cache for
        Hamlet, and we end by checking the cache to ensure that his
        realm appears to be deactivated.  You can make this test fail
        by disabling cache.flush_realm()."""
        hamlet_id = self.example_user("hamlet").id
        user = get_user_profile_by_id(hamlet_id)
        realm = get_realm("zulip")
        iago = self.example_user("iago")
        do_change_realm_subdomain(realm, "newzulip", acting_user=iago)
        user = get_user_profile_by_id(hamlet_id)
        self.assertEqual(user.realm.string_id, "newzulip")

        placeholder_realm = get_realm("zulip")
        self.assertTrue(placeholder_realm.deactivated)
        self.assertEqual(placeholder_realm.deactivated_redirect, user.realm.url)

        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_SUBDOMAIN_CHANGED, acting_user=iago
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {"old_subdomain": "zulip", "new_subdomain": "newzulip"}
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)

    def test_do_deactivate_realm_clears_scheduled_jobs(self) -> None:
        user = self.example_user("hamlet")
        send_future_email(
            "zerver/emails/onboarding_zulip_topics",
            user.realm,
            to_user_ids=[user.id],
            delay=timedelta(hours=1),
        )
        self.assertEqual(ScheduledEmail.objects.count(), 1)
        do_deactivate_realm(
            user.realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        self.assertEqual(ScheduledEmail.objects.count(), 0)

    def test_do_change_realm_description_clears_cached_descriptions(self) -> None:
        realm = get_realm("zulip")
        rendered_description = get_realm_rendered_description(realm)
        text_description = get_realm_text_description(realm)

        realm.description = "New description"
        realm.save(update_fields=["description"])

        new_rendered_description = get_realm_rendered_description(realm)
        self.assertNotEqual(rendered_description, new_rendered_description)
        self.assertIn(realm.description, new_rendered_description)

        new_text_description = get_realm_text_description(realm)
        self.assertNotEqual(text_description, new_text_description)
        self.assertEqual(realm.description, new_text_description)

    def test_do_deactivate_realm_on_deactivated_realm(self) -> None:
        """Ensure early exit is working in realm deactivation"""
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        self.assertTrue(realm.deactivated)

        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        self.assertTrue(realm.deactivated)

    def test_do_set_deactivated_redirect_on_deactivated_realm(self) -> None:
        """Ensure that the redirect url is working when deactivating realm"""
        realm = get_realm("zulip")

        redirect_url = "new_server.zulip.com"
        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        self.assertTrue(realm.deactivated)
        do_add_deactivated_redirect(realm, redirect_url)
        self.assertEqual(realm.deactivated_redirect, redirect_url)

        new_redirect_url = "test.zulip.com"
        do_add_deactivated_redirect(realm, new_redirect_url)
        self.assertEqual(realm.deactivated_redirect, new_redirect_url)
        self.assertNotEqual(realm.deactivated_redirect, redirect_url)

    def test_do_reactivate_realm(self) -> None:
        realm = get_realm("zulip")
        do_deactivate_realm(
            realm,
            acting_user=None,
            deactivation_reason="owner_request",
            email_owners=False,
            deletion_delay_days=15,
        )
        self.assertTrue(realm.deactivated)

        do_reactivate_realm(realm)
        self.assertFalse(realm.deactivated)
        self.assertEqual(realm.scheduled_deletion_date, None)

        log_entry = RealmAuditLog.objects.last()
        assert log_entry is not None

        self.assertEqual(log_entry.realm, realm)
        self.assertEqual(log_entry.event_type, AuditLogEventType.REALM_REACTIVATED)
        log_entry_id = log_entry.id

        with self.assertLogs(level="WARNING") as m:
            # do_reactivate_realm on a realm that's not deactivated should be a noop.
            do_reactivate_realm(realm)

        self.assertEqual(
            m.output,
            [f"WARNING:root:Realm {realm.id} cannot be reactivated because it is already active."],
        )

        self.assertFalse(realm.deactivated)

        latest_log_entry = RealmAuditLog.objects.last()
        assert latest_log_entry is not None
        self.assertEqual(latest_log_entry.id, log_entry_id)

    def test_realm_reactivation_link(self) -> None:
        realm = get_realm("zulip")
        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        self.assertTrue(realm.deactivated)

        obj = RealmReactivationStatus.objects.create(realm=realm)
        confirmation_url = create_confirmation_link(obj, Confirmation.REALM_REACTIVATION)
        key = confirmation_url.split("/")[-1]
        response = self.client_get(confirmation_url)
        self.assert_in_success_response(["redirect-to-post-form"], response)

        response = self.client_post("/reactivate/", {"key": key})
        self.assert_in_success_response(
            ["Your organization has been successfully reactivated"], response
        )
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

        # Make sure the link can't be reused.
        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        response = self.client_get(confirmation_url)
        self.assertEqual(response.status_code, 404)
        response = self.client_post("/reactivate/", {"key": key})
        self.assertEqual(response.status_code, 404)

    def test_realm_reactivation_confirmation_object(self) -> None:
        realm = get_realm("zulip")
        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        self.assertTrue(realm.deactivated)
        obj = RealmReactivationStatus.objects.create(realm=realm)
        create_confirmation_link(obj, Confirmation.REALM_REACTIVATION)
        confirmation = Confirmation.objects.last()
        assert confirmation is not None
        self.assertEqual(confirmation.content_object, obj)
        self.assertEqual(confirmation.realm, realm)

    def test_do_send_realm_deactivation_email_no_acting_user(self) -> None:
        realm = get_realm("zulip")
        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=True
        )
        self.assertEqual(realm.deactivated, True)
        self.assert_length(mail.outbox, 1)
        self.assertIn(
            "Your Zulip organization Zulip Dev has been deactivated", mail.outbox[0].subject
        )
        self.assertIn("Your Zulip organization, Zulip Dev, was deactivated on", mail.outbox[0].body)

    def test_do_send_realm_deactivation_email_by_support(self) -> None:
        realm = get_realm("lear")
        king = self.lear_user("king")
        king.role = UserProfile.ROLE_REALM_OWNER
        king.save()
        iago = self.example_user("iago")
        do_deactivate_realm(
            realm, acting_user=iago, deactivation_reason="owner_request", email_owners=True
        )
        self.assertEqual(realm.deactivated, True)
        self.assert_length(mail.outbox, 1)
        self.assertIn(
            "Your Zulip organization Lear & Co. has been deactivated", mail.outbox[0].subject
        )
        self.assertIn(
            "Your Zulip organization, Lear & Co., was deactivated on",
            mail.outbox[0].body,
        )

    def test_do_send_realm_deactivation_email_by_owner(self) -> None:
        realm = get_realm("zulip")
        iago = self.example_user("iago")
        iago.role = UserProfile.ROLE_REALM_OWNER
        iago.save(update_fields=["role"])
        do_deactivate_realm(
            realm,
            acting_user=iago,
            deactivation_reason="owner_request",
            deletion_delay_days=14,
            email_owners=True,
        )
        self.assertEqual(realm.deactivated, True)
        self.assert_length(mail.outbox, 2)
        for email in mail.outbox:
            if email.to[0] == "iago@zulip.com":
                self.assertIn(
                    "Your Zulip organization Zulip Dev has been deactivated", email.subject
                )
                self.assertIn(
                    "You have deactivated your Zulip organization, Zulip Dev, on", email.body
                )
            else:
                self.assertIn(
                    "Your Zulip organization Zulip Dev has been deactivated", email.subject
                )
                self.assertIn(
                    "Your Zulip organization, Zulip Dev, was deactivated by Iago on", email.body
                )
            self.assertIn(
                "All data associated with this organization will be permanently deleted on",
                email.body,
            )

    def test_do_send_realm_deactivation_email_with_immediate_data_deletion(self) -> None:
        realm = get_realm("zulip")
        desdemona = self.example_user("desdemona")
        with (
            mock.patch("zerver.actions.realm_settings.do_scrub_realm") as mock_scrub_realm,
            self.assertLogs(level="INFO"),
        ):
            do_deactivate_realm(
                realm,
                acting_user=desdemona,
                deactivation_reason="owner_request",
                deletion_delay_days=0,
                email_owners=True,
            )
            self.assertEqual(realm.deactivated, True)
            mock_scrub_realm.assert_called_once_with(realm, acting_user=None)
            self.assert_length(mail.outbox, 1)
            email = mail.outbox[0]
            self.assertIn("Your Zulip organization Zulip Dev has been deactivated", email.subject)
            self.assertIn("You have deactivated your Zulip organization, Zulip Dev, on", email.body)
            self.assertIn(
                "All data associated with this organization has been permanently deleted.",
                email.body,
            )

    def test_do_send_realm_reactivation_email(self) -> None:
        realm = get_realm("zulip")
        do_deactivate_realm(
            realm, acting_user=None, deactivation_reason="owner_request", email_owners=False
        )
        self.assertEqual(realm.deactivated, True)
        iago = self.example_user("iago")
        do_send_realm_reactivation_email(realm, acting_user=iago)

        self.assert_length(mail.outbox, 1)
        self.assertEqual(self.email_envelope_from(mail.outbox[0]), settings.NOREPLY_EMAIL_ADDRESS)
        self.assertRegex(
            self.email_display_from(mail.outbox[0]),
            rf"^testserver account security <{self.TOKENIZED_NOREPLY_REGEX}>\Z",
        )
        self.assertIn("Reactivate your Zulip organization", mail.outbox[0].subject)
        self.assertIn("Dear former administrators", mail.outbox[0].body)
        admins = realm.get_human_admin_users()
        confirmation_url = self.get_confirmation_url_from_outbox(admins[0].delivery_email)
        key = confirmation_url.split("/")[-1]

        response = self.client_get(confirmation_url)
        self.assert_in_success_response(["redirect-to-post-form"], response)

        response = self.client_post("/reactivate/", {"key": key})
        self.assert_in_success_response(
            ["Your organization has been successfully reactivated"], response
        )
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)
        self.assertEqual(
            RealmAuditLog.objects.filter(
                event_type=AuditLogEventType.REALM_REACTIVATION_EMAIL_SENT, acting_user=iago
            ).count(),
            1,
        )

    def test_realm_reactivation_with_random_link(self) -> None:
        random_link = "/reactivate/5e89081eb13984e0f3b130bf7a4121d153f1614b"
        response = self.client_get(random_link)
        self.assertEqual(response.status_code, 404)
        self.assert_in_response(
            "The organization reactivation link has expired or is not valid.", response
        )

    def test_change_new_stream_announcements_stream(self) -> None:
        # We need an admin user.
        self.login("iago")

        disabled_notif_stream_id = -1
        req = dict(
            new_stream_announcements_stream_id=orjson.dumps(disabled_notif_stream_id).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.new_stream_announcements_stream, None)

        new_notif_stream_id = Stream.objects.get(name="Denmark").id
        req = dict(new_stream_announcements_stream_id=orjson.dumps(new_notif_stream_id).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.new_stream_announcements_stream is not None
        self.assertEqual(realm.new_stream_announcements_stream.id, new_notif_stream_id)

        # Test that admin can set the setting to an unsubscribed private stream as well.
        new_notif_stream_id = self.make_stream("private_stream", invite_only=True).id
        req = dict(new_stream_announcements_stream_id=orjson.dumps(new_notif_stream_id).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.new_stream_announcements_stream is not None
        self.assertEqual(realm.new_stream_announcements_stream.id, new_notif_stream_id)

        invalid_notif_stream_id = 1234
        req = dict(
            new_stream_announcements_stream_id=orjson.dumps(invalid_notif_stream_id).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Invalid channel ID")
        realm = get_realm("zulip")
        assert realm.new_stream_announcements_stream is not None
        self.assertNotEqual(realm.new_stream_announcements_stream.id, invalid_notif_stream_id)

    def test_get_default_new_stream_announcements_stream(self) -> None:
        realm = get_realm("zulip")
        verona = get_stream("verona", realm)

        new_stream_announcements_stream = realm.new_stream_announcements_stream
        assert new_stream_announcements_stream is not None
        self.assertEqual(new_stream_announcements_stream.id, verona.id)
        do_deactivate_stream(new_stream_announcements_stream, acting_user=None)
        realm.refresh_from_db()
        self.assertIsNone(realm.new_stream_announcements_stream)

    def test_merge_streams(self) -> None:
        realm = get_realm("zulip")
        denmark = get_stream("Denmark", realm)
        cordelia = self.example_user("cordelia")
        new_stream_announcements_stream = realm.new_stream_announcements_stream
        assert new_stream_announcements_stream is not None
        new_stream_announcements_stream_messages_count = Message.objects.filter(
            realm_id=realm.id, recipient=new_stream_announcements_stream.recipient
        ).count()

        create_stream_if_needed(realm, "Atlantis")
        self.subscribe(cordelia, "Atlantis")
        self.send_stream_message(cordelia, "Atlantis")
        atlantis = get_stream("Atlantis", realm)

        stats = merge_streams(realm, denmark, denmark)
        self.assertEqual(stats, (0, 0, 0))

        stats = merge_streams(realm, denmark, atlantis)
        self.assertEqual(stats, (1, 1, 1))

        self.assertEqual(get_stream("Atlantis", realm).deactivated, True)

        stats = merge_streams(realm, denmark, new_stream_announcements_stream)
        realm.refresh_from_db()
        self.assertEqual(stats, (2, new_stream_announcements_stream_messages_count, 10))
        self.assertIsNone(realm.new_stream_announcements_stream)

    def test_change_signup_announcements_stream(self) -> None:
        # We need an admin user.
        self.login("iago")

        disabled_signup_announcements_stream_id = -1
        req = dict(
            signup_announcements_stream_id=orjson.dumps(
                disabled_signup_announcements_stream_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.signup_announcements_stream, None)

        new_signup_announcements_stream_id = Stream.objects.get(name="Denmark").id
        req = dict(
            signup_announcements_stream_id=orjson.dumps(new_signup_announcements_stream_id).decode()
        )

        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.signup_announcements_stream is not None
        self.assertEqual(realm.signup_announcements_stream.id, new_signup_announcements_stream_id)

        # Test that admin can set the setting to an unsubscribed private stream as well.
        new_signup_announcements_stream_id = self.make_stream("private_stream", invite_only=True).id
        req = dict(
            signup_announcements_stream_id=orjson.dumps(new_signup_announcements_stream_id).decode()
        )

        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.signup_announcements_stream is not None
        self.assertEqual(realm.signup_announcements_stream.id, new_signup_announcements_stream_id)

        invalid_signup_announcements_stream_id = 1234
        req = dict(
            signup_announcements_stream_id=orjson.dumps(
                invalid_signup_announcements_stream_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Invalid channel ID")
        realm = get_realm("zulip")
        assert realm.signup_announcements_stream is not None
        self.assertNotEqual(
            realm.signup_announcements_stream.id, invalid_signup_announcements_stream_id
        )

    def test_get_default_signup_announcements_stream(self) -> None:
        realm = get_realm("zulip")
        verona = get_stream("verona", realm)
        realm.signup_announcements_stream = verona
        realm.save(update_fields=["signup_announcements_stream"])

        signup_announcements_stream = realm.signup_announcements_stream
        assert signup_announcements_stream is not None
        self.assertEqual(signup_announcements_stream, verona)
        do_deactivate_stream(signup_announcements_stream, acting_user=None)
        realm.refresh_from_db()
        self.assertIsNone(realm.signup_announcements_stream)

    def test_change_zulip_update_announcements_stream(self) -> None:
        # We need an admin user.
        self.login("iago")

        disabled_zulip_update_announcements_stream_id = -1
        req = dict(
            zulip_update_announcements_stream_id=orjson.dumps(
                disabled_zulip_update_announcements_stream_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.zulip_update_announcements_stream, None)

        new_zulip_update_announcements_stream_id = Stream.objects.get(name="Denmark").id
        req = dict(
            zulip_update_announcements_stream_id=orjson.dumps(
                new_zulip_update_announcements_stream_id
            ).decode()
        )

        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.zulip_update_announcements_stream is not None
        self.assertEqual(
            realm.zulip_update_announcements_stream.id, new_zulip_update_announcements_stream_id
        )

        # Test that admin can set the setting to an unsubscribed private stream as well.
        new_zulip_update_announcements_stream_id = self.make_stream(
            "private_stream", invite_only=True
        ).id
        req = dict(
            zulip_update_announcements_stream_id=orjson.dumps(
                new_zulip_update_announcements_stream_id
            ).decode()
        )

        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.zulip_update_announcements_stream is not None
        self.assertEqual(
            realm.zulip_update_announcements_stream.id, new_zulip_update_announcements_stream_id
        )

        invalid_zulip_update_announcements_stream_id = 1234
        req = dict(
            zulip_update_announcements_stream_id=orjson.dumps(
                invalid_zulip_update_announcements_stream_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Invalid channel ID")
        realm = get_realm("zulip")
        assert realm.zulip_update_announcements_stream is not None
        self.assertNotEqual(
            realm.zulip_update_announcements_stream.id, invalid_zulip_update_announcements_stream_id
        )

    def test_get_default_zulip_update_announcements_stream(self) -> None:
        realm = get_realm("zulip")
        verona = get_stream("verona", realm)
        realm.zulip_update_announcements_stream = verona
        realm.save(update_fields=["zulip_update_announcements_stream"])

        zulip_update_announcements_stream = realm.zulip_update_announcements_stream
        assert zulip_update_announcements_stream is not None
        self.assertEqual(zulip_update_announcements_stream, verona)
        do_deactivate_stream(zulip_update_announcements_stream, acting_user=None)
        realm.refresh_from_db()
        self.assertIsNone(realm.zulip_update_announcements_stream)

    def test_change_moderation_request_channel(self) -> None:
        # We need an admin user.
        self.login("iago")

        disabled_moderation_request_channel_id = -1
        req = dict(
            moderation_request_channel_id=orjson.dumps(
                disabled_moderation_request_channel_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.moderation_request_channel, None)

        # Test that admin can set the setting to a private stream.
        new_moderation_request_channel_id = self.make_stream("private_stream", invite_only=True).id
        req = dict(
            moderation_request_channel_id=orjson.dumps(new_moderation_request_channel_id).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.moderation_request_channel is not None
        self.assertEqual(realm.moderation_request_channel.id, new_moderation_request_channel_id)

        invalid_moderation_request_channel_id = 4321
        req = dict(
            moderation_request_channel_id=orjson.dumps(
                invalid_moderation_request_channel_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Invalid channel ID")
        realm = get_realm("zulip")
        assert realm.moderation_request_channel is not None
        self.assertNotEqual(
            realm.moderation_request_channel.id, invalid_moderation_request_channel_id
        )

        # Test that setting this to public channel should fail.
        public_moderation_request_channel_id = Stream.objects.get(name="Denmark").id
        req = dict(
            moderation_request_channel_id=orjson.dumps(
                public_moderation_request_channel_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Moderation request channel must be private.")
        realm = get_realm("zulip")
        assert realm.moderation_request_channel is not None
        self.assertNotEqual(
            realm.moderation_request_channel.id, public_moderation_request_channel_id
        )

    def test_get_default_moderation_request_channel(self) -> None:
        realm = get_realm("zulip")
        verona = get_stream("verona", realm)
        realm.moderation_request_channel = verona
        realm.save(update_fields=["moderation_request_channel"])

        moderation_request_channel = realm.moderation_request_channel
        assert moderation_request_channel is not None
        self.assertEqual(moderation_request_channel, verona)
        do_deactivate_stream(moderation_request_channel, acting_user=None)
        realm.refresh_from_db()
        self.assertIsNone(realm.moderation_request_channel)

    def test_change_realm_default_language(self) -> None:
        # we need an admin user.
        self.login("iago")
        # Test to make sure that when invalid languages are passed
        # as the default realm language, correct validation error is
        # raised and the invalid language is not saved in db
        invalid_lang = "invalid_lang"
        req = dict(default_language=invalid_lang)
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, f"Invalid language '{invalid_lang}'")
        realm = get_realm("zulip")
        self.assertNotEqual(realm.default_language, invalid_lang)

    def test_deactivate_realm_by_owner(self) -> None:
        self.login("desdemona")
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

        result = self.client_post("/json/realm/deactivate")
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertTrue(realm.deactivated)

    def test_deactivate_realm_by_non_owner(self) -> None:
        self.login("iago")
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

        result = self.client_post("/json/realm/deactivate")
        self.assert_json_error(result, "Must be an organization owner")
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

    def test_invalid_integer_attribute_values(self) -> None:
        integer_values = [
            key for key, value in Realm.property_types.items() if value in (int, int | None)
        ]

        invalid_values = dict(
            message_retention_days=10,
            video_chat_provider=10,
            giphy_rating=10,
            waiting_period_threshold=-10,
            digest_weekday=10,
            message_content_delete_limit_seconds=-10,
            message_edit_history_visibility_policy=10,
            message_content_edit_limit_seconds=0,
            move_messages_within_stream_limit_seconds=0,
            move_messages_between_streams_limit_seconds=0,
        )

        # We need an admin user.
        self.login("iago")

        for name in integer_values:
            invalid_value = invalid_values.get(name)
            if invalid_value is None:
                raise AssertionError(f"No test created for {name}")

            self.do_test_invalid_integer_attribute_value(name, invalid_value)

    def do_test_invalid_integer_attribute_value(self, val_name: str, invalid_val: int) -> None:
        possible_messages = {
            f"Invalid {val_name}",
            f"Bad value for '{val_name}'",
            f"Bad value for '{val_name}': {invalid_val}",
            f"Invalid {val_name} {invalid_val}",
            f"{val_name} is too small",
            "Must be an organization owner",
        }

        req = {val_name: invalid_val}
        result = self.client_patch("/json/realm", req)
        msg = self.get_json_error(result)
        self.assertTrue(msg in possible_messages)

    def test_change_video_chat_provider(self) -> None:
        self.assertEqual(
            get_realm("zulip").video_chat_provider, Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"]
        )
        self.login("iago")

        invalid_video_chat_provider_value = 10
        req = {"video_chat_provider": orjson.dumps(invalid_video_chat_provider_value).decode()}
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(
            result, f"Invalid video_chat_provider {invalid_video_chat_provider_value}"
        )

        req = {
            "video_chat_provider": orjson.dumps(
                Realm.VIDEO_CHAT_PROVIDERS["disabled"]["id"]
            ).decode()
        }
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        self.assertEqual(
            get_realm("zulip").video_chat_provider, Realm.VIDEO_CHAT_PROVIDERS["disabled"]["id"]
        )

        req = {
            "video_chat_provider": orjson.dumps(
                Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"]
            ).decode()
        }
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        self.assertEqual(
            get_realm("zulip").video_chat_provider, Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"]
        )

        big_blue_button_provider_id = Realm.VIDEO_CHAT_PROVIDERS["big_blue_button"]["id"]
        req = {"video_chat_provider": f"{big_blue_button_provider_id}"}
        with self.settings(BIG_BLUE_BUTTON_SECRET=None):
            result = self.client_patch("/json/realm", req)
            self.assert_json_error(
                result, f"Invalid video_chat_provider {big_blue_button_provider_id}"
            )

        with self.settings(BIG_BLUE_BUTTON_URL=None):
            result = self.client_patch("/json/realm", req)
            self.assert_json_error(
                result, f"Invalid video_chat_provider {big_blue_button_provider_id}"
            )

        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        self.assertEqual(
            get_realm("zulip").video_chat_provider,
            big_blue_button_provider_id,
        )

        zoom_provider_id = Realm.VIDEO_CHAT_PROVIDERS["zoom"]["id"]
        req = {"video_chat_provider": f"{zoom_provider_id}"}
        with self.settings(VIDEO_ZOOM_SERVER_TO_SERVER_ACCOUNT_ID=None, VIDEO_ZOOM_CLIENT_ID=None):
            result = self.client_patch("/json/realm", req)
            self.assert_json_error(result, f"Invalid video_chat_provider {zoom_provider_id}")

        with self.settings(
            VIDEO_ZOOM_SERVER_TO_SERVER_ACCOUNT_ID=None, VIDEO_ZOOM_CLIENT_SECRET=None
        ):
            result = self.client_patch("/json/realm", req)
            self.assert_json_error(result, f"Invalid video_chat_provider {zoom_provider_id}")

        with self.settings(VIDEO_ZOOM_SERVER_TO_SERVER_ACCOUNT_ID=None):
            result = self.client_patch("/json/realm", req)
            self.assert_json_success(result)
            self.assertEqual(
                get_realm("zulip").video_chat_provider,
                zoom_provider_id,
            )

        zoom_server_to_server_provider_id = Realm.VIDEO_CHAT_PROVIDERS["zoom_server_to_server"][
            "id"
        ]
        req = {"video_chat_provider": f"{zoom_server_to_server_provider_id}"}
        with self.settings(VIDEO_ZOOM_CLIENT_ID=None):
            result = self.client_patch("/json/realm", req)
            self.assert_json_error(
                result, f"Invalid video_chat_provider {zoom_server_to_server_provider_id}"
            )

        with self.settings(VIDEO_ZOOM_CLIENT_SECRET=None):
            result = self.client_patch("/json/realm", req)
            self.assert_json_error(
                result, f"Invalid video_chat_provider {zoom_server_to_server_provider_id}"
            )

        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        self.assertEqual(
            get_realm("zulip").video_chat_provider,
            zoom_server_to_server_provider_id,
        )

    def test_data_deletion_schedule_when_deactivating_realm(self) -> None:
        self.login("desdemona")

        # settings.MIN_DEACTIVATED_REALM_DELETION_DAYS have default value 14.
        # So minimum 14 days should be given for data deletion.
        result = self.client_post("/json/realm/deactivate", {"deletion_delay_days": 12})
        self.assert_json_error(result, "Data deletion time must be at least 14 days in the future.")

        result = self.client_post("/json/realm/deactivate", {"deletion_delay_days": 17})
        self.assert_json_success(result)

        do_reactivate_realm(get_realm("zulip"))

        with self.settings(MIN_DEACTIVATED_REALM_DELETION_DAYS=None):
            self.login("desdemona")

            result = self.client_post("/json/realm/deactivate", {"deletion_delay_days": 12})
            self.assert_json_success(result)

        do_reactivate_realm(get_realm("zulip"))

        with self.settings(MAX_DEACTIVATED_REALM_DELETION_DAYS=30):
            self.login("desdemona")

            # None value to deletion_delay_days means data will be never deleted.
            result = self.client_post(
                "/json/realm/deactivate", {"deletion_delay_days": orjson.dumps(None).decode()}
            )
            self.assert_json_error(
                result,
                "Data deletion time must be at most 30 days in the future.",
            )

            result = self.client_post("/json/realm/deactivate", {"deletion_delay_days": 40})
            self.assert_json_error(
                result,
                "Data deletion time must be at most 30 days in the future.",
            )

            result = self.client_post("/json/realm/deactivate", {"deletion_delay_days": 25})
            self.assert_json_success(result)

    def test_scrub_deactivated_realms(self) -> None:
        zulip = get_realm("zulip")
        zephyr = get_realm("zephyr")
        lear = get_realm("lear")

        do_deactivate_realm(
            zephyr,
            acting_user=None,
            deletion_delay_days=3,
            deactivation_reason="owner_request",
            email_owners=False,
        )
        self.assertTrue(zephyr.deactivated)

        do_deactivate_realm(
            zulip,
            acting_user=None,
            deletion_delay_days=None,
            deactivation_reason="owner_request",
            email_owners=False,
        )
        self.assertTrue(zulip.deactivated)

        with mock.patch("zerver.actions.realm_settings.do_scrub_realm") as mock_scrub_realm:
            clean_deactivated_realm_data()
            mock_scrub_realm.assert_not_called()

        with (
            mock.patch("zerver.actions.realm_settings.do_scrub_realm") as mock_scrub_realm,
            self.assertLogs(level="INFO"),
        ):
            do_deactivate_realm(
                lear,
                acting_user=None,
                deletion_delay_days=0,
                deactivation_reason="owner_request",
                email_owners=False,
            )
            self.assertTrue(lear.deactivated)
            mock_scrub_realm.assert_called_once_with(lear, acting_user=None)

        do_reactivate_realm(lear)

        with (
            time_machine.travel(timezone_now() + timedelta(days=4), tick=False),
            mock.patch("zerver.actions.realm_settings.do_scrub_realm") as mock_scrub_realm,
            self.assertLogs(level="INFO"),
        ):
            clean_deactivated_realm_data()
            mock_scrub_realm.assert_called_once_with(zephyr, acting_user=None)

    def test_delete_expired_demo_organizations(self) -> None:
        zulip = get_realm("zulip")
        assert not zulip.deactivated
        assert zulip.demo_organization_scheduled_deletion_date is None

        with mock.patch(
            "zerver.actions.realm_settings.do_deactivate_realm"
        ) as mock_deactivate_realm:
            delete_expired_demo_organizations()
            mock_deactivate_realm.assert_not_called()

        # Add scheduled demo organization deletion date
        zulip.demo_organization_scheduled_deletion_date = timezone_now() + timedelta(days=4)
        zulip.save()

        # Before deletion date
        with mock.patch(
            "zerver.actions.realm_settings.do_deactivate_realm"
        ) as mock_deactivate_realm:
            delete_expired_demo_organizations()
            mock_deactivate_realm.assert_not_called()

        # After deletion date, when owner email is set.
        with (
            time_machine.travel(timezone_now() + timedelta(days=5), tick=False),
            mock.patch(
                "zerver.actions.realm_settings.do_deactivate_realm"
            ) as mock_deactivate_realm,
        ):
            delete_expired_demo_organizations()
            mock_deactivate_realm.assert_called_once_with(
                realm=zulip,
                acting_user=None,
                deactivation_reason="demo_expired",
                deletion_delay_days=0,
                email_owners=True,
            )

        # After deletion date, when owner email is not set.
        desdemona = self.example_user("desdemona")
        desdemona.delivery_email = ""
        desdemona.save()

        with (
            time_machine.travel(timezone_now() + timedelta(days=5), tick=False),
            mock.patch(
                "zerver.actions.realm_settings.do_deactivate_realm"
            ) as mock_deactivate_realm,
        ):
            delete_expired_demo_organizations()
            mock_deactivate_realm.assert_called_once_with(
                realm=zulip,
                acting_user=None,
                deactivation_reason="demo_expired",
                deletion_delay_days=0,
                email_owners=False,
            )

    def test_initial_plan_type(self) -> None:
        with self.settings(BILLING_ENABLED=True):
            self.assertEqual(do_create_realm("hosted", "hosted").plan_type, Realm.PLAN_TYPE_LIMITED)
            self.assertEqual(
                get_realm("hosted").max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX
            )
            self.assertEqual(
                get_realm("hosted").message_visibility_limit, Realm.MESSAGE_VISIBILITY_LIMITED
            )
            self.assertEqual(get_realm("hosted").upload_quota_gb, Realm.UPLOAD_QUOTA_LIMITED)

        with self.settings(BILLING_ENABLED=False):
            self.assertEqual(
                do_create_realm("onpremise", "onpremise").plan_type, Realm.PLAN_TYPE_SELF_HOSTED
            )
            self.assertEqual(
                get_realm("onpremise").max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX
            )
            self.assertEqual(get_realm("onpremise").message_visibility_limit, None)
            self.assertEqual(get_realm("onpremise").upload_quota_gb, None)

    def test_initial_auth_methods(self) -> None:
        with self.settings(
            BILLING_ENABLED=True,
            DEVELOPMENT=False,
            AUTHENTICATION_BACKENDS=(
                "zproject.backends.EmailAuthBackend",
                "zproject.backends.AzureADAuthBackend",
                "zproject.backends.SAMLAuthBackend",
            ),
        ):
            # Test a Cloud-like realm creation.
            # Only the auth backends available on the free plan should be enabled.
            realm = do_create_realm("hosted", "hosted")
            self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_LIMITED)

            self.assertEqual(
                realm.authentication_methods_dict(),
                {
                    "Email": True,
                    "AzureAD": False,
                    "SAML": False,
                },
            )

            # Now make sure that a self-hosted server creates realms with all auth methods enabled.
            with self.settings(BILLING_ENABLED=False):
                realm = do_create_realm("onpremise", "onpremise")
                self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_SELF_HOSTED)

                self.assertEqual(
                    realm.authentication_methods_dict(),
                    {
                        "Email": True,
                        "AzureAD": True,
                        "SAML": True,
                    },
                )

    def test_change_org_type(self) -> None:
        realm = get_realm("zulip")
        iago = self.example_user("iago")
        self.assertEqual(realm.org_type, Realm.ORG_TYPES["business"]["id"])

        do_change_realm_org_type(realm, Realm.ORG_TYPES["government"]["id"], acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_ORG_TYPE_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {
            "old_value": Realm.ORG_TYPES["business"]["id"],
            "new_value": Realm.ORG_TYPES["government"]["id"],
        }
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.org_type, Realm.ORG_TYPES["government"]["id"])

    def test_change_realm_max_invites(self) -> None:
        realm = get_realm("zulip")
        iago = self.example_user("iago")
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_SELF_HOSTED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)

        do_change_realm_max_invites(realm, 1, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_PROPERTY_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {
            "old_value": settings.INVITES_DEFAULT_REALM_DAILY_MAX,
            "new_value": 1,
            "property": "max_invites",
        }
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_SELF_HOSTED)
        self.assertEqual(realm.max_invites, 1)

        do_change_realm_max_invites(realm, 0, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_PROPERTY_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {"old_value": 1, "new_value": None, "property": "max_invites"}
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_SELF_HOSTED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)

        realm.plan_type = Realm.PLAN_TYPE_PLUS
        realm.save()

        do_change_realm_max_invites(realm, 0, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_PROPERTY_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {
            "old_value": settings.INVITES_DEFAULT_REALM_DAILY_MAX,
            "new_value": Realm.INVITES_STANDARD_REALM_DAILY_MAX,
            "property": "max_invites",
        }
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_PLUS)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)

        realm.plan_type = Realm.PLAN_TYPE_LIMITED
        realm.save()

        do_change_realm_max_invites(realm, 0, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_PROPERTY_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {
            "old_value": Realm.INVITES_STANDARD_REALM_DAILY_MAX,
            "new_value": settings.INVITES_DEFAULT_REALM_DAILY_MAX,
            "property": "max_invites",
        }
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_LIMITED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)

        realm.plan_type = Realm.PLAN_TYPE_STANDARD
        realm.save()

        do_change_realm_max_invites(realm, 0, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_PROPERTY_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {
            "old_value": settings.INVITES_DEFAULT_REALM_DAILY_MAX,
            "new_value": Realm.INVITES_STANDARD_REALM_DAILY_MAX,
            "property": "max_invites",
        }
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_STANDARD)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)

        realm.plan_type = Realm.PLAN_TYPE_STANDARD_FREE
        realm.save()

        do_change_realm_max_invites(realm, 50000, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_PROPERTY_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {
            "old_value": Realm.INVITES_STANDARD_REALM_DAILY_MAX,
            "new_value": 50000,
            "property": "max_invites",
        }
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_STANDARD_FREE)
        self.assertEqual(realm.max_invites, 50000)

        do_change_realm_max_invites(realm, 0, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_PROPERTY_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {
            "old_value": 50000,
            "new_value": Realm.INVITES_STANDARD_REALM_DAILY_MAX,
            "property": "max_invites",
        }
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_STANDARD_FREE)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)

    @skipUnless(settings.ZILENCER_ENABLED, "requires zilencer")
    def test_change_realm_plan_type(self) -> None:
        realm = get_realm("zulip")

        # Create additional user, so that the realm has a lot of seats for the purposes
        # of upload quota calculation.
        for count in range(10):
            do_create_user(
                f"email{count}@example.com",
                f"password {count}",
                realm,
                "name",
                role=UserProfile.ROLE_MEMBER,
                acting_user=None,
            )

        iago = self.example_user("iago")
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_SELF_HOSTED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(realm.upload_quota_gb, None)

        members_system_group = NamedUserGroup.objects.get(name=SystemGroups.MEMBERS, realm=realm)
        do_change_realm_permission_group_setting(
            realm, "can_access_all_users_group", members_system_group, acting_user=None
        )
        self.assertEqual(realm.can_access_all_users_group_id, members_system_group.id)

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_STANDARD, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=AuditLogEventType.REALM_PLAN_TYPE_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {
            "old_value": Realm.PLAN_TYPE_SELF_HOSTED,
            "new_value": Realm.PLAN_TYPE_STANDARD,
        }
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_STANDARD)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(
            realm.upload_quota_gb, get_seat_count(realm) * settings.UPLOAD_QUOTA_PER_USER_GB
        )
        everyone_system_group = NamedUserGroup.objects.get(name=SystemGroups.EVERYONE, realm=realm)
        self.assertEqual(realm.can_access_all_users_group_id, everyone_system_group.id)

        do_set_realm_property(realm, "enable_spectator_access", True, acting_user=None)
        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_LIMITED, acting_user=iago)
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_LIMITED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, Realm.MESSAGE_VISIBILITY_LIMITED)
        self.assertEqual(realm.upload_quota_gb, Realm.UPLOAD_QUOTA_LIMITED)
        self.assertFalse(realm.enable_spectator_access)

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_STANDARD_FREE, acting_user=iago)
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_STANDARD_FREE)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(realm.upload_quota_gb, Realm.UPLOAD_QUOTA_STANDARD_FREE)

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_LIMITED, acting_user=iago)
        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_PLUS, acting_user=iago)
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_PLUS)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(
            realm.upload_quota_gb, get_seat_count(realm) * settings.UPLOAD_QUOTA_PER_USER_GB
        )

        do_change_realm_permission_group_setting(
            realm, "can_access_all_users_group", members_system_group, acting_user=None
        )
        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_STANDARD, acting_user=iago)
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_STANDARD)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(
            realm.upload_quota_gb, get_seat_count(realm) * settings.UPLOAD_QUOTA_PER_USER_GB
        )
        self.assertEqual(realm.can_access_all_users_group_id, everyone_system_group.id)

        # Test that custom_upload_quota_gb overrides the default upload_quota_gb
        # implied by a plan and makes .upload_quota_gb be unaffacted by plan changes.
        realm.custom_upload_quota_gb = 100
        realm.save(update_fields=["custom_upload_quota_gb"])
        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_PLUS, acting_user=iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_PLUS)
        self.assertEqual(realm.upload_quota_gb, 100)

        realm.custom_upload_quota_gb = None
        realm.save(update_fields=["custom_upload_quota_gb"])

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_SELF_HOSTED, acting_user=iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_SELF_HOSTED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(realm.upload_quota_gb, None)

    @override_settings(
        BILLING_ENABLED=True,
        AUTHENTICATION_BACKENDS=(
            "zproject.backends.EmailAuthBackend",
            "zproject.backends.AzureADAuthBackend",
            "zproject.backends.SAMLAuthBackend",
        ),
    )
    def test_realm_authentication_methods_after_downgrade(self) -> None:
        realm = get_realm("zulip")
        iago = self.example_user("iago")

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_STANDARD, acting_user=iago)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_STANDARD)

        do_set_realm_authentication_methods(
            realm, {"Email": True, "AzureAD": True, "SAML": True}, acting_user=None
        )

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_LIMITED, acting_user=iago)
        realm.refresh_from_db()
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_LIMITED)

        self.assertEqual(
            realm.authentication_methods_dict(),
            {
                "Email": True,
                "AzureAD": False,
                "SAML": False,
            },
        )

    def test_message_retention_days(self) -> None:
        self.login("iago")
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_SELF_HOSTED)

        req = dict(message_retention_days=orjson.dumps(10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Must be an organization owner")

        self.login("desdemona")

        req = dict(message_retention_days=orjson.dumps(0).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Bad value for 'message_retention_days': 0")

        req = dict(message_retention_days=orjson.dumps(-10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Bad value for 'message_retention_days': -10")

        req = dict(message_retention_days=orjson.dumps("invalid").decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Bad value for 'message_retention_days': invalid")

        req = dict(message_retention_days=orjson.dumps(-1).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Bad value for 'message_retention_days': -1")

        req = dict(message_retention_days=orjson.dumps("unlimited").decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)

        req = dict(message_retention_days=orjson.dumps(10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_LIMITED, acting_user=None)
        req = dict(message_retention_days=orjson.dumps(10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Available on Zulip Cloud Standard. Upgrade to access.")

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_STANDARD, acting_user=None)
        req = dict(message_retention_days=orjson.dumps(10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)

    def test_jitsi_server_url(self) -> None:
        self.login("iago")
        realm = get_realm("zulip")
        self.assertEqual(realm.video_chat_provider, Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"])

        req = dict(jitsi_server_url=orjson.dumps("").decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "jitsi_server_url is not an allowed_type")

        req = dict(jitsi_server_url=orjson.dumps("invalidURL").decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "jitsi_server_url is not an allowed_type")

        req = dict(jitsi_server_url=orjson.dumps(12).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "jitsi_server_url is not a string")

        url_string = "".join(random.choices(string.ascii_lowercase, k=180))
        long_url = "https://jitsi.example.com/" + url_string
        req = dict(jitsi_server_url=orjson.dumps(long_url).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "jitsi_server_url is not an allowed_type")

        valid_url = "https://jitsi.example.com"
        req = dict(jitsi_server_url=orjson.dumps(valid_url).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.jitsi_server_url, valid_url)

        req = dict(jitsi_server_url=orjson.dumps("default").decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.jitsi_server_url, None)

    def test_do_create_realm(self) -> None:
        realm = do_create_realm("realm_string_id", "realm name")

        self.assertEqual(realm.string_id, "realm_string_id")
        self.assertEqual(realm.name, "realm name")
        self.assertFalse(realm.emails_restricted_to_domains)
        self.assertEqual(realm.description, "")
        self.assertTrue(realm.invite_required)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_LIMITED)
        self.assertEqual(realm.org_type, Realm.ORG_TYPES["unspecified"]["id"])
        self.assertEqual(type(realm.date_created), datetime)

        self.assertTrue(
            RealmAuditLog.objects.filter(
                realm=realm,
                event_type=AuditLogEventType.REALM_CREATED,
                event_time=realm.date_created,
            ).exists()
        )

        assert realm.new_stream_announcements_stream is not None
        self.assertEqual(realm.new_stream_announcements_stream.name, "general")
        self.assertEqual(realm.new_stream_announcements_stream.realm, realm)

        self.assertIsNone(realm.signup_announcements_stream)

        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_LIMITED)

        for (
            setting_name,
            permission_configuration,
        ) in Realm.REALM_PERMISSION_GROUP_SETTINGS.items():
            self.assertEqual(
                getattr(realm, setting_name).named_user_group.name,
                permission_configuration.default_group_name,
            )

    def test_do_create_realm_with_keyword_arguments(self) -> None:
        date_created = timezone_now() - timedelta(days=100)
        realm = do_create_realm(
            "realm_string_id",
            "realm name",
            emails_restricted_to_domains=True,
            date_created=date_created,
            description="realm description",
            invite_required=False,
            plan_type=Realm.PLAN_TYPE_STANDARD_FREE,
            org_type=Realm.ORG_TYPES["community"]["id"],
            enable_read_receipts=True,
        )
        self.assertEqual(realm.string_id, "realm_string_id")
        self.assertEqual(realm.name, "realm name")
        self.assertTrue(realm.emails_restricted_to_domains)
        self.assertEqual(realm.description, "realm description")
        self.assertFalse(realm.invite_required)
        self.assertEqual(realm.plan_type, Realm.PLAN_TYPE_STANDARD_FREE)
        self.assertEqual(realm.org_type, Realm.ORG_TYPES["community"]["id"])
        self.assertEqual(realm.date_created, date_created)
        self.assertEqual(realm.enable_read_receipts, True)

        self.assertTrue(
            RealmAuditLog.objects.filter(
                realm=realm,
                event_type=AuditLogEventType.REALM_CREATED,
                event_time=realm.date_created,
            ).exists()
        )

        assert realm.new_stream_announcements_stream is not None
        self.assertEqual(realm.new_stream_announcements_stream.name, "general")
        self.assertEqual(realm.new_stream_announcements_stream.realm, realm)

        self.assertIsNone(realm.signup_announcements_stream)

    def test_realm_is_web_public(self) -> None:
        realm = get_realm("zulip")
        # By default "Rome" is web_public in zulip realm
        rome = Stream.objects.get(name="Rome")
        self.assertEqual(rome.is_web_public, True)
        self.assertEqual(realm.has_web_public_streams(), True)
        self.assertEqual(realm.web_public_streams_enabled(), True)

        with self.settings(WEB_PUBLIC_STREAMS_ENABLED=False):
            self.assertEqual(realm.has_web_public_streams(), False)
            self.assertEqual(realm.web_public_streams_enabled(), False)

        realm.enable_spectator_access = False
        realm.save()
        self.assertEqual(realm.has_web_public_streams(), False)
        self.assertEqual(realm.web_public_streams_enabled(), False)

        realm.enable_spectator_access = True
        realm.save()

        # Convert Rome to a public stream
        rome.is_web_public = False
        rome.save()
        self.assertEqual(Stream.objects.filter(realm=realm, is_web_public=True).count(), 0)
        self.assertEqual(realm.web_public_streams_enabled(), True)
        self.assertEqual(realm.has_web_public_streams(), False)
        with self.settings(WEB_PUBLIC_STREAMS_ENABLED=False):
            self.assertEqual(realm.web_public_streams_enabled(), False)
            self.assertEqual(realm.has_web_public_streams(), False)

        # Restore state
        rome.is_web_public = True
        rome.save()
        self.assertEqual(Stream.objects.filter(realm=realm, is_web_public=True).count(), 1)
        self.assertEqual(realm.has_web_public_streams(), True)
        self.assertEqual(realm.web_public_streams_enabled(), True)
        with self.settings(WEB_PUBLIC_STREAMS_ENABLED=False):
            self.assertEqual(realm.web_public_streams_enabled(), False)
            self.assertEqual(realm.has_web_public_streams(), False)

        realm.plan_type = Realm.PLAN_TYPE_LIMITED
        realm.save()
        self.assertEqual(Stream.objects.filter(realm=realm, is_web_public=True).count(), 1)
        self.assertEqual(realm.web_public_streams_enabled(), False)
        self.assertEqual(realm.has_web_public_streams(), False)
        with self.settings(WEB_PUBLIC_STREAMS_ENABLED=False):
            self.assertEqual(realm.web_public_streams_enabled(), False)
            self.assertEqual(realm.has_web_public_streams(), False)

    def test_creating_realm_creates_system_groups(self) -> None:
        realm = do_create_realm("realm_string_id", "realm name")
        system_user_groups = NamedUserGroup.objects.filter(realm=realm, is_system_group=True)

        self.assert_length(system_user_groups, 8)
        user_group_names = [group.name for group in system_user_groups]
        expected_system_group_names = [
            SystemGroups.OWNERS,
            SystemGroups.ADMINISTRATORS,
            SystemGroups.MODERATORS,
            SystemGroups.FULL_MEMBERS,
            SystemGroups.MEMBERS,
            SystemGroups.EVERYONE,
            SystemGroups.EVERYONE_ON_INTERNET,
            SystemGroups.NOBODY,
        ]
        self.assertEqual(sorted(user_group_names), sorted(expected_system_group_names))

    @activate_push_notification_service()
    def test_do_create_realm_notify_bouncer(self) -> None:
        dummy_send_realms_only_response = {
            "result": "success",
            "msg": "",
            "realms": {},
        }
        with mock.patch(
            "zerver.lib.remote_server.send_to_push_bouncer",
        ) as m:
            get_response = {
                "last_realm_count_id": 0,
                "last_installation_count_id": 0,
                "last_realmauditlog_id": 0,
            }

            def mock_send_to_push_bouncer_response(method: str, *args: Any) -> dict[str, Any]:
                if method == "GET":
                    return get_response
                return dummy_send_realms_only_response

            m.side_effect = mock_send_to_push_bouncer_response

            with self.captureOnCommitCallbacks(execute=True):
                realm = do_create_realm("realm_string_id", "realm name")

        self.assertEqual(realm.string_id, "realm_string_id")
        self.assertEqual(m.call_count, 2)

        calls_args_for_assert = m.call_args_list[1][0]
        self.assertEqual(calls_args_for_assert[0], "POST")
        self.assertEqual(calls_args_for_assert[1], "server/analytics")
        self.assertIn(
            realm.id, [realm["id"] for realm in json.loads(m.call_args_list[1][0][2]["realms"])]
        )

    def test_changing_waiting_period_updates_system_groups(self) -> None:
        realm = get_realm("zulip")
        members_system_group = NamedUserGroup.objects.get(
            realm=realm, name=SystemGroups.MEMBERS, is_system_group=True
        )
        full_members_system_group = NamedUserGroup.objects.get(
            realm=realm, name=SystemGroups.FULL_MEMBERS, is_system_group=True
        )

        self.assert_length(UserGroupMembership.objects.filter(user_group=members_system_group), 9)
        self.assert_length(
            UserGroupMembership.objects.filter(user_group=full_members_system_group), 9
        )
        self.assertEqual(realm.waiting_period_threshold, 0)

        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        prospero = self.example_user("prospero")
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=hamlet
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=othello
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=prospero
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=hamlet
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=othello
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=prospero
            ).exists()
        )

        hamlet.date_joined = timezone_now() - timedelta(days=50)
        hamlet.save()
        othello.date_joined = timezone_now() - timedelta(days=75)
        othello.save()
        prospero.date_joined = timezone_now() - timedelta(days=150)
        prospero.save()
        do_set_realm_property(realm, "waiting_period_threshold", 100, acting_user=None)

        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=hamlet
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=othello
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=prospero
            ).exists()
        )
        self.assertFalse(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=hamlet
            ).exists()
        )
        self.assertFalse(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=othello
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=prospero
            ).exists()
        )

        do_set_realm_property(realm, "waiting_period_threshold", 70, acting_user=None)
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=hamlet
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=othello
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=members_system_group, user_profile=prospero
            ).exists()
        )
        self.assertFalse(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=hamlet
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=othello
            ).exists()
        )
        self.assertTrue(
            UserGroupMembership.objects.filter(
                user_group=full_members_system_group, user_profile=prospero
            ).exists()
        )


class RealmAPITest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.login("desdemona")

    def process_value_for_enum_settings(self, raw_value: Any) -> tuple[Any, Any]:
        if isinstance(raw_value, Enum):
            api_value = raw_value.name
            value = raw_value.value
        else:
            api_value = raw_value
            value = raw_value
        return (api_value, value)

    def update_with_api(self, name: str, value: int | str) -> Realm:
        if not isinstance(value, str):
            value = orjson.dumps(value).decode()
        result = self.client_patch("/json/realm", {name: value})
        self.assert_json_success(result)
        return get_realm("zulip")  # refresh data

    def do_test_realm_update_api(self, name: str) -> None:
        """Test updating realm properties.

        If new realm properties have been added to the Realm model but the
        test_values dict below has not been updated, this will raise an
        assertion error.
        """

        bool_tests: list[bool] = [False, True]
        test_values: dict[str, Any] = dict(
            default_language=["de", "en"],
            default_code_block_language=["javascript", ""],
            description=["Realm description", "New description"],
            digest_weekday=[0, 1, 2],
            message_edit_history_visibility_policy=Realm.MESSAGE_EDIT_HISTORY_VISIBILITY_POLICY_TYPES,
            message_retention_days=[10, 20],
            name=["Zulip", "New Name"],
            waiting_period_threshold=[10, 20],
            video_chat_provider=[
                Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"],
                Realm.VIDEO_CHAT_PROVIDERS["disabled"]["id"],
            ],
            jitsi_server_url=["https://example.jit.si"],
            giphy_rating=[
                Realm.GIPHY_RATING_OPTIONS["y"]["id"],
                Realm.GIPHY_RATING_OPTIONS["r"]["id"],
            ],
            message_content_delete_limit_seconds=[1000, 1100, 1200],
            message_content_edit_limit_seconds=[1000, 1100, 1200],
            move_messages_within_stream_limit_seconds=[1000, 1100, 1200],
            move_messages_between_streams_limit_seconds=[1000, 1100, 1200],
            topics_policy=Realm.REALM_TOPICS_POLICY_TYPES,
        )

        vals = test_values.get(name)
        if Realm.property_types[name] is bool:
            vals = bool_tests
        if vals is None:
            raise AssertionError(f"No test created for {name}")

        if name == "jitsi_server_url":
            realm = get_realm("zulip")
            self.assertIsNone(realm.jitsi_server_url, None)
            realm = self.update_with_api(name, orjson.dumps(vals[0]).decode())
            self.assertEqual(realm.jitsi_server_url, vals[0])

            realm = self.update_with_api(name, orjson.dumps("default").decode())
            self.assertIsNone(realm.jitsi_server_url, None)
            return

        do_set_realm_property(get_realm("zulip"), name, vals[0], acting_user=None)

        for val in vals[1:]:
            api_value, value = self.process_value_for_enum_settings(val)
            realm = self.update_with_api(name, api_value)
            self.assertEqual(getattr(realm, name), value)

        api_value, value = self.process_value_for_enum_settings(vals[0])
        realm = self.update_with_api(name, api_value)
        self.assertEqual(getattr(realm, name), value)

    def do_test_realm_permission_group_setting_update_api(self, setting_name: str) -> None:
        realm = get_realm("zulip")

        all_system_user_groups = NamedUserGroup.objects.filter(
            realm=realm,
            is_system_group=True,
        )

        setting_permission_configuration = Realm.REALM_PERMISSION_GROUP_SETTINGS[setting_name]

        default_group_name = setting_permission_configuration.default_group_name
        default_group = all_system_user_groups.get(name=default_group_name)

        self.assertEqual(getattr(realm, setting_name), default_group.usergroup_ptr)

        for user_group in all_system_user_groups:
            value = orjson.dumps(
                {
                    "new": user_group.id,
                }
            ).decode()

            if (
                (
                    user_group.name == SystemGroups.EVERYONE_ON_INTERNET
                    and not setting_permission_configuration.allow_internet_group
                )
                or (
                    user_group.name == SystemGroups.NOBODY
                    and not setting_permission_configuration.allow_nobody_group
                )
                or (
                    user_group.name == SystemGroups.EVERYONE
                    and not setting_permission_configuration.allow_everyone_group
                )
                or (
                    setting_permission_configuration.allowed_system_groups
                    and user_group.name
                    not in setting_permission_configuration.allowed_system_groups
                )
            ):
                result = self.client_patch("/json/realm", {setting_name: value})
                self.assert_json_error(
                    result, f"'{setting_name}' setting cannot be set to '{user_group.name}' group."
                )
                continue

            realm = self.update_with_api(setting_name, value)
            self.assertEqual(getattr(realm, setting_name), user_group.usergroup_ptr)

        if setting_permission_configuration.require_system_group:
            leadership_group = NamedUserGroup.objects.get(name="leadership", realm=realm)

            value = orjson.dumps(
                {
                    "new": leadership_group.id,
                }
            ).decode()

            result = self.client_patch("/json/realm", {setting_name: value})
            self.assert_json_error(result, f"'{setting_name}' must be a system user group.")

            admins_group = NamedUserGroup.objects.get(name=SystemGroups.ADMINISTRATORS, realm=realm)
            moderators_group = NamedUserGroup.objects.get(name=SystemGroups.MODERATORS, realm=realm)
            value = orjson.dumps(
                {
                    "new": {
                        "direct_members": [],
                        "direct_subgroups": [admins_group.id, leadership_group.id],
                    }
                }
            ).decode()
            result = self.client_patch("/json/realm", {setting_name: value})
            self.assert_json_error(result, f"'{setting_name}' must be a system user group.")

            value = orjson.dumps(
                {
                    "new": {
                        "direct_members": [],
                        "direct_subgroups": [admins_group.id, moderators_group.id],
                    }
                }
            ).decode()
            result = self.client_patch("/json/realm", {setting_name: value})
            self.assert_json_error(result, f"'{setting_name}' must be a system user group.")

            group = admins_group
            if setting_permission_configuration.allowed_system_groups:
                group = NamedUserGroup.objects.get(
                    name=setting_permission_configuration.allowed_system_groups[0], realm=realm
                )

            value = orjson.dumps(
                {
                    "new": {
                        "direct_members": [],
                        "direct_subgroups": [group.id],
                    }
                }
            ).decode()
            realm = self.update_with_api(setting_name, value)
            self.assertEqual(getattr(realm, setting_name), group.usergroup_ptr)

    def do_test_realm_permission_group_setting_update_api_with_anonymous_groups(
        self, setting_name: str
    ) -> None:
        realm = get_realm("zulip")
        othello = self.example_user("othello")
        hamlet = self.example_user("hamlet")
        leadership_group = NamedUserGroup.objects.get(name="leadership", realm=realm)

        moderators_group = NamedUserGroup.objects.get(
            name=SystemGroups.MODERATORS, realm=realm, is_system_group=True
        )

        result = self.client_patch(
            "/json/realm", {setting_name: orjson.dumps({"new": moderators_group.id}).decode()}
        )
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(getattr(realm, setting_name), moderators_group.usergroup_ptr)

        # Try passing the old value as well.
        admins_group = NamedUserGroup.objects.get(
            name=SystemGroups.ADMINISTRATORS, realm=realm, is_system_group=True
        )
        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {"new": admins_group.id, "old": leadership_group.id}
                ).decode()
            },
        )
        self.assert_json_error(result, "'old' value does not match the expected value.")

        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {
                        "new": admins_group.id,
                        "old": {
                            "direct_members": [othello.id],
                            "direct_subgroups": [moderators_group.id],
                        },
                    }
                ).decode(),
            },
        )
        self.assert_json_error(result, "'old' value does not match the expected value.")

        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {"new": admins_group.id, "old": moderators_group.id}
                ).decode()
            },
        )
        realm = get_realm("zulip")
        self.assertEqual(getattr(realm, setting_name), admins_group.usergroup_ptr)

        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {
                        "new": {
                            "direct_members": [othello.id],
                            "direct_subgroups": [leadership_group.id],
                        }
                    }
                ).decode()
            },
        )
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertCountEqual(list(getattr(realm, setting_name).direct_members.all()), [othello])
        self.assertCountEqual(
            list(getattr(realm, setting_name).direct_subgroups.all()), [leadership_group]
        )

        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {
                        "new": {
                            "direct_members": [hamlet.id],
                            "direct_subgroups": [moderators_group.id],
                        },
                        "old": moderators_group.id,
                    }
                ).decode()
            },
        )
        self.assert_json_error(result, "'old' value does not match the expected value.")

        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {
                        "new": {
                            "direct_members": [hamlet.id],
                            "direct_subgroups": [moderators_group.id],
                        },
                        "old": {
                            "direct_members": [othello.id],
                            "direct_subgroups": [moderators_group.id],
                        },
                    }
                ).decode(),
            },
        )
        self.assert_json_error(result, "'old' value does not match the expected value.")

        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {
                        "new": {
                            "direct_members": [hamlet.id],
                            "direct_subgroups": [moderators_group.id],
                        },
                        "old": {
                            "direct_members": [othello.id],
                            "direct_subgroups": [leadership_group.id],
                        },
                    }
                ).decode()
            },
        )
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertCountEqual(list(getattr(realm, setting_name).direct_members.all()), [hamlet])
        self.assertCountEqual(
            list(getattr(realm, setting_name).direct_subgroups.all()), [moderators_group]
        )

        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {
                        "new": leadership_group.id,
                        "old": {
                            "direct_members": [hamlet.id],
                            "direct_subgroups": [moderators_group.id],
                        },
                    }
                ).decode()
            },
        )
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(getattr(realm, setting_name), leadership_group.usergroup_ptr)

        # Test that object with only one direct_subgroup is considered
        # same as passing the named user group ID directly.
        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {
                        "new": {
                            "direct_members": [],
                            "direct_subgroups": [admins_group.id],
                        },
                        "old": {
                            "direct_members": [],
                            "direct_subgroups": [leadership_group.id],
                        },
                    }
                ).decode()
            },
        )
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(getattr(realm, setting_name), admins_group.usergroup_ptr)

        permission_configuration = Realm.REALM_PERMISSION_GROUP_SETTINGS[setting_name]
        nobody_group = NamedUserGroup.objects.get(
            name=SystemGroups.NOBODY, realm=realm, is_system_group=True
        )
        result = self.client_patch(
            "/json/realm",
            {
                setting_name: orjson.dumps(
                    {
                        "new": {
                            "direct_members": [],
                            "direct_subgroups": [],
                        },
                        "old": admins_group.id,
                    }
                ).decode()
            },
        )
        if not permission_configuration.allow_nobody_group:
            self.assert_json_error(
                result, f"'{setting_name}' setting cannot be set to 'role:nobody' group."
            )
        else:
            self.assert_json_success(result)
            realm = get_realm("zulip")
            self.assertEqual(getattr(realm, setting_name), nobody_group.usergroup_ptr)

    def test_update_realm_properties(self) -> None:
        for prop in Realm.property_types:
            # push_notifications_enabled is maintained by the server, not via the API.
            if prop != "push_notifications_enabled":
                with self.subTest(property=prop):
                    self.do_test_realm_update_api(prop)

        hamlet = self.example_user("hamlet")
        check_add_user_group(get_realm("zulip"), "leadership", [hamlet], acting_user=hamlet)
        for prop in Realm.REALM_PERMISSION_GROUP_SETTINGS:
            with self.subTest(property=prop):
                self.do_test_realm_permission_group_setting_update_api(prop)
                if Realm.REALM_PERMISSION_GROUP_SETTINGS[prop].require_system_group:
                    # Anonymous system groups aren't relevant when
                    # restricted to system groups.
                    continue
                self.do_test_realm_permission_group_setting_update_api_with_anonymous_groups(prop)

    # Not in Realm.property_types because org_type has
    # a unique RealmAuditLog event_type.
    def test_update_realm_org_type(self) -> None:
        vals = [t["id"] for t in Realm.ORG_TYPES.values()]

        do_change_realm_org_type(get_realm("zulip"), vals[0], acting_user=None)

        for val in vals[1:]:
            realm = self.update_with_api("org_type", val)
            self.assertEqual(realm.org_type, val)

        realm = self.update_with_api("org_type", vals[0])
        self.assertEqual(realm.org_type, vals[0])

        # Now we test an invalid org_type id.
        invalid_org_type = 1
        assert invalid_org_type not in vals
        result = self.client_patch("/json/realm", {"org_type": invalid_org_type})
        self.assert_json_error(result, "Invalid org_type")

    def test_invalid_edit_history_visibility(self) -> None:
        result = self.client_patch(
            "/json/realm",
            {"message_edit_history_visibility_policy": "invalid"},
        )
        self.assert_json_error(result, "Invalid message_edit_history_visibility_policy")

    def test_invalid_topics_policy(self) -> None:
        result = self.client_patch(
            "/json/realm",
            {"topics_policy": "invalid"},
        )
        self.assert_json_error(result, "Invalid topics_policy")

    def update_with_realm_default_api(self, name: str, val: Any) -> None:
        if not isinstance(val, str):
            val = orjson.dumps(val).decode()
        result = self.client_patch("/json/realm/user_settings_defaults", {name: val})
        self.assert_json_success(result)

    def do_test_realm_default_setting_update_api(self, name: str) -> None:
        bool_tests: list[bool] = [False, True]
        test_values: dict[str, Any] = dict(
            web_font_size_px=[UserProfile.WEB_FONT_SIZE_PX_COMPACT],
            web_line_height_percent=[UserProfile.WEB_LINE_HEIGHT_PERCENT_COMPACT],
            color_scheme=UserProfile.COLOR_SCHEME_CHOICES,
            web_home_view=["recent_topics", "inbox", "all_messages"],
            emojiset=[emojiset["key"] for emojiset in RealmUserDefault.emojiset_choices()],
            demote_inactive_streams=UserProfile.DEMOTE_STREAMS_CHOICES,
            web_mark_read_on_scroll_policy=UserProfile.WEB_MARK_READ_ON_SCROLL_POLICY_CHOICES,
            web_channel_default_view=UserProfile.WEB_CHANNEL_DEFAULT_VIEW_CHOICES,
            user_list_style=UserProfile.USER_LIST_STYLE_CHOICES,
            web_animate_image_previews=["always", "on_hover", "never"],
            web_stream_unreads_count_display_policy=UserProfile.WEB_STREAM_UNREADS_COUNT_DISPLAY_POLICY_CHOICES,
            desktop_icon_count_display=UserProfile.DESKTOP_ICON_COUNT_DISPLAY_CHOICES,
            notification_sound=["zulip", "ding"],
            email_notifications_batching_period_seconds=[120, 300],
            email_address_visibility=UserProfile.EMAIL_ADDRESS_VISIBILITY_TYPES,
            realm_name_in_email_notifications_policy=UserProfile.REALM_NAME_IN_EMAIL_NOTIFICATIONS_POLICY_CHOICES,
            automatically_follow_topics_policy=UserProfile.AUTOMATICALLY_CHANGE_VISIBILITY_POLICY_CHOICES,
            automatically_unmute_topics_in_muted_streams_policy=UserProfile.AUTOMATICALLY_CHANGE_VISIBILITY_POLICY_CHOICES,
            automatically_follow_topics_where_mentioned=[True, False],
            resolved_topic_notice_auto_read_policy=UserProfile.RESOLVED_TOPIC_NOTICE_AUTO_READ_POLICY_TYPES,
        )

        vals = test_values.get(name)
        property_type = RealmUserDefault.property_types[name]

        if property_type is bool:
            vals = bool_tests

        if vals is None:
            raise AssertionError(f"No test created for {name}")

        realm = get_realm("zulip")
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        do_set_realm_user_default_setting(realm_user_default, name, vals[0], acting_user=None)

        for val in vals[1:]:
            api_value, value = self.process_value_for_enum_settings(val)
            self.update_with_realm_default_api(name, api_value)
            realm_user_default = RealmUserDefault.objects.get(realm=realm)
            self.assertEqual(getattr(realm_user_default, name), value)

        api_value, value = self.process_value_for_enum_settings(vals[0])
        self.update_with_realm_default_api(name, api_value)
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        self.assertEqual(getattr(realm_user_default, name), value)

    def test_update_default_realm_settings(self) -> None:
        for prop in RealmUserDefault.property_types:
            # enable_marketing_emails setting is not actually used and thus cannot be updated
            # using this endpoint. It is included in notification_setting_types only for avoiding
            # duplicate code. default_language is currently present in Realm table also and thus
            # is updated using '/realm' endpoint, but this will be removed in future and the
            # settings in RealmUserDefault table will be used.
            if prop in [
                "default_language",
                "enable_login_emails",
                "enable_marketing_emails",
                "allow_private_data_export",
            ]:
                continue
            self.do_test_realm_default_setting_update_api(prop)

    def test_update_default_information_density_settings(self) -> None:
        realm = get_realm("zulip")

        # Start with the legacy settings configuration
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        realm_user_default.web_font_size_px = RealmUserDefault.WEB_FONT_SIZE_PX_COMPACT
        realm_user_default.web_line_height_percent = (
            RealmUserDefault.WEB_LINE_HEIGHT_PERCENT_COMPACT
        )
        realm_user_default.save()
        self.login("iago")

        data = {"web_font_size_px": 16}
        result = self.client_patch("/json/realm/user_settings_defaults", data)
        self.assert_json_success(result)
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        self.assertEqual(realm_user_default.web_font_size_px, 16)

        data = {"web_font_size_px": 20}
        result = self.client_patch("/json/realm/user_settings_defaults", data)
        self.assert_json_success(result)
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        self.assertEqual(realm_user_default.web_font_size_px, 20)

        data = {"web_line_height_percent": 140}
        result = self.client_patch("/json/realm/user_settings_defaults", data)
        self.assert_json_success(result)
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        self.assertEqual(realm_user_default.web_line_height_percent, 140)

        data = {"web_line_height_percent": 130}
        result = self.client_patch("/json/realm/user_settings_defaults", data)
        self.assert_json_success(result)
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        self.assertEqual(realm_user_default.web_line_height_percent, 130)

        data = {
            "web_font_size_px": 14,
            "web_line_height_percent": 122,
        }
        result = self.client_patch("/json/realm/user_settings_defaults", data)
        self.assert_json_success(result)
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        self.assertEqual(realm_user_default.web_font_size_px, 14)
        self.assertEqual(realm_user_default.web_line_height_percent, 122)

    def test_invalid_default_notification_sound_value(self) -> None:
        result = self.client_patch(
            "/json/realm/user_settings_defaults", {"notification_sound": "invalid"}
        )
        self.assert_json_error(result, "Invalid notification sound 'invalid'")

        result = self.client_patch(
            "/json/realm/user_settings_defaults", {"notification_sound": "zulip"}
        )
        self.assert_json_success(result)
        realm = get_realm("zulip")
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        self.assertEqual(realm_user_default.notification_sound, "zulip")

    def test_invalid_email_notifications_batching_period_setting(self) -> None:
        result = self.client_patch(
            "/json/realm/user_settings_defaults",
            {"email_notifications_batching_period_seconds": -1},
        )
        self.assert_json_error(result, "Invalid email batching period: -1 seconds")

        result = self.client_patch(
            "/json/realm/user_settings_defaults",
            {"email_notifications_batching_period_seconds": 7 * 24 * 60 * 60 + 10},
        )
        self.assert_json_error(result, "Invalid email batching period: 604810 seconds")

    def test_invalid_emojiset_value(self) -> None:
        result = self.client_patch("/json/realm/user_settings_defaults", {"emojiset": "invalid"})
        self.assert_json_error(
            result, "Invalid emojiset: Value error, Not in the list of possible values"
        )

    def test_invalid_resolved_topic_notice_auto_read_policy(self) -> None:
        result = self.client_patch(
            "/json/realm/user_settings_defaults",
            {"resolved_topic_notice_auto_read_policy": "invalid"},
        )
        self.assert_json_error(result, "Invalid resolved_topic_notice_auto_read_policy")

    def test_ignored_parameters_in_realm_default_endpoint(self) -> None:
        params = {"starred_message_counts": orjson.dumps(False).decode(), "emoji_set": "twitter"}
        result = self.client_patch("/json/realm/user_settings_defaults", params)
        self.assert_json_success(result, ignored_parameters=["emoji_set"])

        realm = get_realm("zulip")
        realm_user_default = RealmUserDefault.objects.get(realm=realm)
        self.assertEqual(realm_user_default.starred_message_counts, False)

    def test_update_realm_move_messages_within_stream_limit_seconds_unlimited_value(self) -> None:
        realm = get_realm("zulip")
        self.login("iago")
        realm = self.update_with_api(
            "move_messages_within_stream_limit_seconds", orjson.dumps("unlimited").decode()
        )
        self.assertEqual(realm.move_messages_within_stream_limit_seconds, None)

    def test_update_realm_move_messages_between_streams_limit_seconds_unlimited_value(self) -> None:
        realm = get_realm("zulip")
        self.login("iago")
        realm = self.update_with_api(
            "move_messages_between_streams_limit_seconds", orjson.dumps("unlimited").decode()
        )
        self.assertEqual(realm.move_messages_between_streams_limit_seconds, None)

    def do_test_changing_settings_by_owners_only(self, setting_name: str) -> None:
        bool_tests: list[bool] = [False, True]
        test_values: dict[str, Any] = dict(
            waiting_period_threshold=[10, 20],
        )

        vals = test_values.get(setting_name)
        if Realm.property_types[setting_name] is bool:
            vals = bool_tests
        assert vals is not None

        do_set_realm_property(get_realm("zulip"), setting_name, vals[0], acting_user=None)
        value = vals[1]

        if not isinstance(value, str):
            value = orjson.dumps(value).decode()

        self.login("iago")
        result = self.client_patch("/json/realm", {setting_name: value})
        self.assert_json_error(result, "Must be an organization owner")

        self.login("desdemona")
        result = self.client_patch("/json/realm", {setting_name: value})
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(getattr(realm, setting_name), vals[1])

    def test_changing_user_joining_settings_require_owners(self) -> None:
        self.do_test_changing_settings_by_owners_only("invite_required")
        self.do_test_changing_settings_by_owners_only("emails_restricted_to_domains")
        self.do_test_changing_settings_by_owners_only("disallow_disposable_email_addresses")
        self.do_test_changing_settings_by_owners_only("waiting_period_threshold")

    def do_test_changing_groups_setting_by_owners_only(self, setting_name: str) -> None:
        realm = get_realm("zulip")
        admins_group = NamedUserGroup.objects.get(
            name=SystemGroups.ADMINISTRATORS, realm=realm, is_system_group=True
        )

        self.login("iago")
        result = self.client_patch(
            "/json/realm", {setting_name: orjson.dumps({"new": admins_group.id}).decode()}
        )
        self.assert_json_error(result, "Must be an organization owner")

        self.login("desdemona")
        result = self.client_patch(
            "/json/realm", {setting_name: orjson.dumps({"new": admins_group.id}).decode()}
        )
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(getattr(realm, setting_name).id, admins_group.id)

    def test_can_create_groups_setting_requires_owner(self) -> None:
        self.do_test_changing_groups_setting_by_owners_only("can_create_groups")

    def test_can_invite_users_group_setting_requires_owner(self) -> None:
        self.do_test_changing_groups_setting_by_owners_only("can_invite_users_group")

    def test_can_manage_all_groups_setting_requires_owner(self) -> None:
        self.do_test_changing_groups_setting_by_owners_only("can_manage_all_groups")

    def test_enable_spectator_access_for_limited_plan_realms(self) -> None:
        self.login("iago")
        realm = get_realm("zulip")
        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_LIMITED, acting_user=None)
        self.assertFalse(realm.enable_spectator_access)

        req = {"enable_spectator_access": orjson.dumps(True).decode()}
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Available on Zulip Cloud Standard. Upgrade to access.")

    def test_can_create_groups_limited_plan_realms(self) -> None:
        self.login("iago")
        realm = get_realm("zulip")
        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_LIMITED, acting_user=None)

        members_group = NamedUserGroup.objects.get(name="role:members", realm=realm)
        req = {"can_create_groups": orjson.dumps({"new": members_group.id}).decode()}
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Available on Zulip Cloud Standard. Upgrade to access.")

    def test_changing_can_access_all_users_group_based_on_plan_type(self) -> None:
        realm = get_realm("zulip")
        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_LIMITED, acting_user=None)
        self.login("iago")

        members_group = NamedUserGroup.objects.get(name="role:members", realm=realm)
        req = {"can_access_all_users_group": orjson.dumps({"new": members_group.id}).decode()}
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Available on Zulip Cloud Plus. Upgrade to access.")

        do_change_realm_plan_type(realm, Realm.PLAN_TYPE_STANDARD, acting_user=None)
        req = {"can_access_all_users_group": orjson.dumps({"new": members_group.id}).decode()}
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Available on Zulip Cloud Plus. Upgrade to access.")


class ScrubRealmTest(ZulipTestCase):
    def test_do_delete_all_realm_attachments(self) -> None:
        realm = get_realm("zulip")
        hamlet = self.example_user("hamlet")
        Attachment.objects.filter(realm=realm).delete()
        assert settings.LOCAL_UPLOADS_DIR is not None
        assert settings.LOCAL_FILES_DIR is not None

        path_ids = []
        for n in range(1, 4):
            content = f"content{n}".encode()
            url = upload_message_attachment(f"dummy{n}.txt", "text/plain", content, hamlet)[0]
            base = "/user_uploads/"
            self.assertEqual(base, url[: len(base)])
            path_id = re.sub(r"/user_uploads/", "", url)
            self.assertTrue(os.path.isfile(os.path.join(settings.LOCAL_FILES_DIR, path_id)))
            path_ids.append(path_id)

        with mock.patch(
            "zerver.actions.realm_settings.delete_message_attachments",
            side_effect=delete_message_attachments,
        ) as p:
            do_delete_all_realm_attachments(realm, batch_size=2)

            self.assertEqual(p.call_count, 2)
            p.assert_has_calls(
                [
                    mock.call([path_ids[0], path_ids[1]]),
                    mock.call([path_ids[2]]),
                ]
            )
        self.assertEqual(Attachment.objects.filter(realm=realm).count(), 0)
        for file_path in path_ids:
            self.assertFalse(os.path.isfile(os.path.join(settings.LOCAL_FILES_DIR, path_id)))

    def test_scrub_realm(self) -> None:
        zulip = get_realm("zulip")
        lear = get_realm("lear")
        internal_realm = get_realm(settings.SYSTEM_BOT_REALM)

        hamlet = self.example_user("hamlet")
        iago = self.example_user("iago")
        othello = self.example_user("othello")

        cordelia = self.lear_user("cordelia")
        king = self.lear_user("king")

        notification_bot = get_system_bot(settings.NOTIFICATION_BOT, internal_realm.id)

        create_stream_if_needed(lear, "Shakespeare")

        self.subscribe(cordelia, "Shakespeare")
        self.subscribe(king, "Shakespeare")

        Message.objects.all().delete()
        UserMessage.objects.all().delete()

        for i in range(5):
            self.send_stream_message(iago, "Scotland")
            self.send_stream_message(othello, "Scotland")
            self.send_stream_message(cordelia, "Shakespeare")
            self.send_stream_message(king, "Shakespeare")

        internal_send_stream_message(
            notification_bot, get_stream("Scotland", zulip), "test", "test"
        )
        internal_send_private_message(notification_bot, othello, "test")
        internal_send_group_direct_message(
            zulip, notification_bot, "test", emails=[othello.email, iago.email]
        )

        internal_send_stream_message(
            notification_bot, get_stream("Shakespeare", lear), "test", "test"
        )
        internal_send_private_message(notification_bot, king, "test")
        internal_send_group_direct_message(
            lear, notification_bot, "test", emails=[cordelia.email, king.email]
        )

        Attachment.objects.filter(realm=zulip).delete()
        Attachment.objects.filter(realm=lear).delete()
        assert settings.LOCAL_UPLOADS_DIR is not None
        assert settings.LOCAL_FILES_DIR is not None
        file_paths = []
        for n, owner in enumerate([iago, othello, hamlet, cordelia, king]):
            content = f"content{n}".encode()
            url = upload_message_attachment(f"dummy{n}.txt", "text/plain", content, owner)[0]
            base = "/user_uploads/"
            self.assertEqual(base, url[: len(base)])
            file_path = os.path.join(settings.LOCAL_FILES_DIR, re.sub(r"/user_uploads/", "", url))
            self.assertTrue(os.path.isfile(file_path))
            file_paths.append(file_path)

        CustomProfileField.objects.create(realm=lear)

        self.assertEqual(
            Message.objects.filter(
                realm_id__in=(zulip.id, lear.id), sender__in=[iago, othello]
            ).count(),
            10,
        )
        self.assertEqual(
            Message.objects.filter(
                realm_id__in=(zulip.id, lear.id), sender__in=[cordelia, king]
            ).count(),
            10,
        )
        self.assertEqual(
            Message.objects.filter(
                realm_id__in=(zulip.id, lear.id), sender=notification_bot
            ).count(),
            6,
        )
        self.assertEqual(UserMessage.objects.filter(user_profile__in=[iago, othello]).count(), 25)
        self.assertEqual(UserMessage.objects.filter(user_profile__in=[cordelia, king]).count(), 25)

        self.assertNotEqual(CustomProfileField.objects.filter(realm=zulip).count(), 0)

        with self.assertLogs(level="WARNING"):
            do_scrub_realm(zulip, acting_user=None)

        self.assertEqual(
            Message.objects.filter(
                realm_id__in=(zulip.id, lear.id), sender__in=[iago, othello]
            ).count(),
            0,
        )
        self.assertEqual(
            Message.objects.filter(
                realm_id__in=(zulip.id, lear.id), sender__in=[cordelia, king]
            ).count(),
            10,
        )
        self.assertEqual(
            Message.objects.filter(
                realm_id__in=(zulip.id, lear.id), sender=notification_bot
            ).count(),
            3,
        )
        self.assertEqual(UserMessage.objects.filter(user_profile__in=[iago, othello]).count(), 0)
        self.assertEqual(UserMessage.objects.filter(user_profile__in=[cordelia, king]).count(), 25)

        self.assertEqual(Attachment.objects.filter(realm=zulip).count(), 0)
        self.assertEqual(Attachment.objects.filter(realm=lear).count(), 2)

        # Zulip realm files don't exist on disk, Lear ones do
        self.assertFalse(os.path.isfile(file_paths[0]))
        self.assertFalse(os.path.isfile(file_paths[1]))
        self.assertFalse(os.path.isfile(file_paths[2]))
        self.assertTrue(os.path.isfile(file_paths[3]))
        self.assertTrue(os.path.isfile(file_paths[4]))

        self.assertEqual(CustomProfileField.objects.filter(realm=zulip).count(), 0)
        self.assertNotEqual(CustomProfileField.objects.filter(realm=lear).count(), 0)

        zulip_users = UserProfile.objects.filter(realm=zulip)
        for user in zulip_users:
            self.assertRegex(user.full_name, r"^Scrubbed [a-z0-9]{15}$")
            self.assertRegex(user.email, rf"^scrubbed-[a-z0-9]{{15}}@{re.escape(zulip.host)}$")
            self.assertRegex(
                user.delivery_email, rf"^scrubbed-[a-z0-9]{{15}}@{re.escape(zulip.host)}$"
            )

        lear_users = UserProfile.objects.filter(realm=lear)
        for user in lear_users:
            self.assertNotRegex(user.full_name, r"^Scrubbed [a-z0-9]{15}$")
            self.assertNotRegex(user.email, rf"^scrubbed-[a-z0-9]{{15}}@{re.escape(zulip.host)}$")
            self.assertNotRegex(
                user.delivery_email, rf"^scrubbed-[a-z0-9]{{15}}@{re.escape(zulip.host)}$"
            )
