"""flow views tests"""
from unittest.mock import MagicMock, PropertyMock, patch

from django.http import HttpRequest, HttpResponse
from django.test import TestCase
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils.encoding import force_str

from authentik.core.models import User
from authentik.flows.challenge import ChallengeTypes
from authentik.flows.exceptions import FlowNonApplicableException
from authentik.flows.markers import ReevaluateMarker, StageMarker
from authentik.flows.models import Flow, FlowDesignation, FlowStageBinding
from authentik.flows.planner import FlowPlan, FlowPlanner
from authentik.flows.stage import PLAN_CONTEXT_PENDING_USER_IDENTIFIER, StageView
from authentik.flows.views import NEXT_ARG_NAME, SESSION_KEY_PLAN, FlowExecutorView
from authentik.lib.config import CONFIG
from authentik.policies.dummy.models import DummyPolicy
from authentik.policies.models import PolicyBinding
from authentik.policies.types import PolicyResult
from authentik.stages.dummy.models import DummyStage

POLICY_RETURN_FALSE = PropertyMock(return_value=PolicyResult(False))
POLICY_RETURN_TRUE = MagicMock(return_value=PolicyResult(True))


def to_stage_response(request: HttpRequest, source: HttpResponse):
    """Mock for to_stage_response that returns the original response, so we can check
    inheritance and member attributes"""
    return source


TO_STAGE_RESPONSE_MOCK = MagicMock(side_effect=to_stage_response)


class TestFlowExecutor(TestCase):
    """Test views logic"""

    def setUp(self):
        self.request_factory = RequestFactory()

    @patch(
        "authentik.flows.views.to_stage_response",
        TO_STAGE_RESPONSE_MOCK,
    )
    def test_existing_plan_diff_flow(self):
        """Check that a plan for a different flow cancels the current plan"""
        flow = Flow.objects.create(
            name="test-existing-plan-diff",
            slug="test-existing-plan-diff",
            designation=FlowDesignation.AUTHENTICATION,
        )
        stage = DummyStage.objects.create(name="dummy")
        binding = FlowStageBinding.objects.create(target=flow, stage=stage)
        plan = FlowPlan(
            flow_pk=flow.pk.hex + "a", bindings=[binding], markers=[StageMarker()]
        )
        session = self.client.session
        session[SESSION_KEY_PLAN] = plan
        session.save()

        cancel_mock = MagicMock()
        with patch("authentik.flows.views.FlowExecutorView.cancel", cancel_mock):
            response = self.client.get(
                reverse("authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}),
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(cancel_mock.call_count, 2)

    @patch(
        "authentik.flows.views.to_stage_response",
        TO_STAGE_RESPONSE_MOCK,
    )
    @patch(
        "authentik.policies.engine.PolicyEngine.result",
        POLICY_RETURN_FALSE,
    )
    def test_invalid_non_applicable_flow(self):
        """Tests that a non-applicable flow returns the correct error message"""
        flow = Flow.objects.create(
            name="test-non-applicable",
            slug="test-non-applicable",
            designation=FlowDesignation.AUTHENTICATION,
        )

        CONFIG.update_from_dict({"domain": "testserver"})
        response = self.client.get(
            reverse("authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            force_str(response.content),
            {
                "component": "ak-stage-access-denied",
                "error_message": FlowNonApplicableException.__doc__,
                "flow_info": {
                    "background": flow.background_url,
                    "cancel_url": reverse("authentik_flows:cancel"),
                    "title": "",
                },
                "type": ChallengeTypes.NATIVE.value,
            },
        )

    @patch(
        "authentik.flows.views.to_stage_response",
        TO_STAGE_RESPONSE_MOCK,
    )
    def test_invalid_empty_flow(self):
        """Tests that an empty flow returns the correct error message"""
        flow = Flow.objects.create(
            name="test-empty",
            slug="test-empty",
            designation=FlowDesignation.AUTHENTICATION,
        )

        CONFIG.update_from_dict({"domain": "testserver"})
        response = self.client.get(
            reverse("authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("authentik_core:root-redirect"))

    @patch(
        "authentik.flows.views.to_stage_response",
        TO_STAGE_RESPONSE_MOCK,
    )
    def test_invalid_flow_redirect(self):
        """Tests that an invalid flow still redirects"""
        flow = Flow.objects.create(
            name="test-empty",
            slug="test-empty",
            designation=FlowDesignation.AUTHENTICATION,
        )

        CONFIG.update_from_dict({"domain": "testserver"})
        dest = "/unique-string"
        url = reverse("authentik_api:flow-executor", kwargs={"flow_slug": flow.slug})
        response = self.client.get(url + f"?{NEXT_ARG_NAME}={dest}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("authentik_core:root-redirect"))

    def test_multi_stage_flow(self):
        """Test a full flow with multiple stages"""
        flow = Flow.objects.create(
            name="test-full",
            slug="test-full",
            designation=FlowDesignation.AUTHENTICATION,
        )
        FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy1"), order=0
        )
        FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy2"), order=1
        )

        exec_url = reverse(
            "authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}
        )
        # First Request, start planning, renders form
        response = self.client.get(exec_url)
        self.assertEqual(response.status_code, 200)
        # Check that two stages are in plan
        session = self.client.session
        plan: FlowPlan = session[SESSION_KEY_PLAN]
        self.assertEqual(len(plan.bindings), 2)
        # Second request, submit form, one stage left
        response = self.client.post(exec_url)
        # Second request redirects to the same URL
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, exec_url)
        # Check that two stages are in plan
        session = self.client.session
        plan: FlowPlan = session[SESSION_KEY_PLAN]
        self.assertEqual(len(plan.bindings), 1)

    @patch(
        "authentik.flows.views.to_stage_response",
        TO_STAGE_RESPONSE_MOCK,
    )
    def test_reevaluate_remove_last(self):
        """Test planner with re-evaluate (last stage is removed)"""
        flow = Flow.objects.create(
            name="test-default-context",
            slug="test-default-context",
            designation=FlowDesignation.AUTHENTICATION,
        )
        false_policy = DummyPolicy.objects.create(result=False, wait_min=1, wait_max=2)

        binding = FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy1"), order=0
        )
        binding2 = FlowStageBinding.objects.create(
            target=flow,
            stage=DummyStage.objects.create(name="dummy2"),
            order=1,
            re_evaluate_policies=True,
        )

        PolicyBinding.objects.create(policy=false_policy, target=binding2, order=0)

        # Here we patch the dummy policy to evaluate to true so the stage is included
        with patch(
            "authentik.policies.dummy.models.DummyPolicy.passes", POLICY_RETURN_TRUE
        ):

            exec_url = reverse(
                "authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}
            )
            # First request, run the planner
            response = self.client.get(exec_url)
            self.assertEqual(response.status_code, 200)

            plan: FlowPlan = self.client.session[SESSION_KEY_PLAN]

            self.assertEqual(plan.bindings[0], binding)
            self.assertEqual(plan.bindings[1], binding2)

            self.assertIsInstance(plan.markers[0], StageMarker)
            self.assertIsInstance(plan.markers[1], ReevaluateMarker)

            # Second request, this passes the first dummy stage
            response = self.client.post(exec_url)
            self.assertEqual(response.status_code, 302)

        # third request, this should trigger the re-evaluate
        # We do this request without the patch, so the policy results in false
        response = self.client.post(exec_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("authentik_core:root-redirect"))

    def test_reevaluate_remove_middle(self):
        """Test planner with re-evaluate (middle stage is removed)"""
        flow = Flow.objects.create(
            name="test-default-context",
            slug="test-default-context",
            designation=FlowDesignation.AUTHENTICATION,
        )
        false_policy = DummyPolicy.objects.create(result=False, wait_min=1, wait_max=2)

        binding = FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy1"), order=0
        )
        binding2 = FlowStageBinding.objects.create(
            target=flow,
            stage=DummyStage.objects.create(name="dummy2"),
            order=1,
            re_evaluate_policies=True,
        )
        binding3 = FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy3"), order=2
        )

        PolicyBinding.objects.create(policy=false_policy, target=binding2, order=0)

        # Here we patch the dummy policy to evaluate to true so the stage is included
        with patch(
            "authentik.policies.dummy.models.DummyPolicy.passes", POLICY_RETURN_TRUE
        ):

            exec_url = reverse(
                "authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}
            )
            # First request, run the planner
            response = self.client.get(exec_url)

            self.assertEqual(response.status_code, 200)
            plan: FlowPlan = self.client.session[SESSION_KEY_PLAN]

            self.assertEqual(plan.bindings[0], binding)
            self.assertEqual(plan.bindings[1], binding2)
            self.assertEqual(plan.bindings[2], binding3)

            self.assertIsInstance(plan.markers[0], StageMarker)
            self.assertIsInstance(plan.markers[1], ReevaluateMarker)
            self.assertIsInstance(plan.markers[2], StageMarker)

            # Second request, this passes the first dummy stage
            response = self.client.post(exec_url)
            self.assertEqual(response.status_code, 302)

            plan: FlowPlan = self.client.session[SESSION_KEY_PLAN]

            self.assertEqual(plan.bindings[0], binding2)
            self.assertEqual(plan.bindings[1], binding3)

            self.assertIsInstance(plan.markers[0], StageMarker)
            self.assertIsInstance(plan.markers[1], StageMarker)

        # third request, this should trigger the re-evaluate
        # We do this request without the patch, so the policy results in false
        response = self.client.post(exec_url)
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            force_str(response.content),
            {
                "component": "xak-flow-redirect",
                "to": reverse("authentik_core:root-redirect"),
                "type": ChallengeTypes.REDIRECT.value,
            },
        )

    def test_reevaluate_keep(self):
        """Test planner with re-evaluate (everything is kept)"""
        flow = Flow.objects.create(
            name="test-default-context",
            slug="test-default-context",
            designation=FlowDesignation.AUTHENTICATION,
        )
        true_policy = DummyPolicy.objects.create(result=True, wait_min=1, wait_max=2)

        binding = FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy1"), order=0
        )
        binding2 = FlowStageBinding.objects.create(
            target=flow,
            stage=DummyStage.objects.create(name="dummy2"),
            order=1,
            re_evaluate_policies=True,
        )
        binding3 = FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy3"), order=2
        )

        PolicyBinding.objects.create(policy=true_policy, target=binding2, order=0)

        # Here we patch the dummy policy to evaluate to true so the stage is included
        with patch(
            "authentik.policies.dummy.models.DummyPolicy.passes", POLICY_RETURN_TRUE
        ):

            exec_url = reverse(
                "authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}
            )
            # First request, run the planner
            response = self.client.get(exec_url)

            self.assertEqual(response.status_code, 200)
            plan: FlowPlan = self.client.session[SESSION_KEY_PLAN]

            self.assertEqual(plan.bindings[0], binding)
            self.assertEqual(plan.bindings[1], binding2)
            self.assertEqual(plan.bindings[2], binding3)

            self.assertIsInstance(plan.markers[0], StageMarker)
            self.assertIsInstance(plan.markers[1], ReevaluateMarker)
            self.assertIsInstance(plan.markers[2], StageMarker)

            # Second request, this passes the first dummy stage
            response = self.client.post(exec_url)
            self.assertEqual(response.status_code, 302)

            plan: FlowPlan = self.client.session[SESSION_KEY_PLAN]

            self.assertEqual(plan.bindings[0], binding2)
            self.assertEqual(plan.bindings[1], binding3)

            self.assertIsInstance(plan.markers[0], StageMarker)
            self.assertIsInstance(plan.markers[1], StageMarker)

            # Third request, this passes the first dummy stage
            response = self.client.post(exec_url)
            self.assertEqual(response.status_code, 302)

            plan: FlowPlan = self.client.session[SESSION_KEY_PLAN]

            self.assertEqual(plan.bindings[0], binding3)

            self.assertIsInstance(plan.markers[0], StageMarker)

        # third request, this should trigger the re-evaluate
        # We do this request without the patch, so the policy results in false
        response = self.client.post(exec_url)
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            force_str(response.content),
            {
                "component": "xak-flow-redirect",
                "to": reverse("authentik_core:root-redirect"),
                "type": ChallengeTypes.REDIRECT.value,
            },
        )

    def test_reevaluate_remove_consecutive(self):
        """Test planner with re-evaluate (consecutive stages are removed)"""
        flow = Flow.objects.create(
            name="test-default-context",
            slug="test-default-context",
            designation=FlowDesignation.AUTHENTICATION,
        )
        false_policy = DummyPolicy.objects.create(result=False, wait_min=1, wait_max=2)

        binding = FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy1"), order=0
        )
        binding2 = FlowStageBinding.objects.create(
            target=flow,
            stage=DummyStage.objects.create(name="dummy2"),
            order=1,
            re_evaluate_policies=True,
        )
        binding3 = FlowStageBinding.objects.create(
            target=flow,
            stage=DummyStage.objects.create(name="dummy3"),
            order=2,
            re_evaluate_policies=True,
        )
        binding4 = FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy4"), order=2
        )

        PolicyBinding.objects.create(policy=false_policy, target=binding2, order=0)
        PolicyBinding.objects.create(policy=false_policy, target=binding3, order=0)

        # Here we patch the dummy policy to evaluate to true so the stage is included
        with patch(
            "authentik.policies.dummy.models.DummyPolicy.passes", POLICY_RETURN_TRUE
        ):

            exec_url = reverse(
                "authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}
            )
            # First request, run the planner
            response = self.client.get(exec_url)
            self.assertEqual(response.status_code, 200)
            self.assertJSONEqual(
                force_str(response.content),
                {
                    "type": ChallengeTypes.NATIVE.value,
                    "component": "ak-stage-dummy",
                    "flow_info": {
                        "background": flow.background_url,
                        "cancel_url": reverse("authentik_flows:cancel"),
                        "title": "",
                    },
                },
            )

            plan: FlowPlan = self.client.session[SESSION_KEY_PLAN]

            self.assertEqual(plan.bindings[0], binding)
            self.assertEqual(plan.bindings[1], binding2)
            self.assertEqual(plan.bindings[2], binding3)
            self.assertEqual(plan.bindings[3], binding4)

            self.assertIsInstance(plan.markers[0], StageMarker)
            self.assertIsInstance(plan.markers[1], ReevaluateMarker)
            self.assertIsInstance(plan.markers[2], ReevaluateMarker)
            self.assertIsInstance(plan.markers[3], StageMarker)

        # Second request, this passes the first dummy stage
        response = self.client.post(exec_url)
        self.assertEqual(response.status_code, 302)

        # third request, this should trigger the re-evaluate
        # A get request will evaluate the policies and this will return stage 4
        # but it won't save it, hence we cant' check the plan
        response = self.client.get(exec_url)
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            force_str(response.content),
            {
                "type": ChallengeTypes.NATIVE.value,
                "component": "ak-stage-dummy",
                "flow_info": {
                    "background": flow.background_url,
                    "cancel_url": reverse("authentik_flows:cancel"),
                    "title": "",
                },
            },
        )

        # fourth request, this confirms the last stage (dummy4)
        # We do this request without the patch, so the policy results in false
        response = self.client.post(exec_url)
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            force_str(response.content),
            {
                "component": "xak-flow-redirect",
                "to": reverse("authentik_core:root-redirect"),
                "type": ChallengeTypes.REDIRECT.value,
            },
        )

    def test_stageview_user_identifier(self):
        """Test PLAN_CONTEXT_PENDING_USER_IDENTIFIER"""
        flow = Flow.objects.create(
            name="test-default-context",
            slug="test-default-context",
            designation=FlowDesignation.AUTHENTICATION,
        )
        FlowStageBinding.objects.create(
            target=flow, stage=DummyStage.objects.create(name="dummy"), order=0
        )

        ident = "test-identifier"

        user = User.objects.create(username="test-user")
        request = self.request_factory.get(
            reverse("authentik_api:flow-executor", kwargs={"flow_slug": flow.slug}),
        )
        request.user = user
        planner = FlowPlanner(flow)
        plan = planner.plan(
            request, default_context={PLAN_CONTEXT_PENDING_USER_IDENTIFIER: ident}
        )

        executor = FlowExecutorView()
        executor.plan = plan
        executor.flow = flow

        stage_view = StageView(executor)
        self.assertEqual(ident, stage_view.get_pending_user(for_display=True).username)
