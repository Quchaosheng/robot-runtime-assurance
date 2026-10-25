"""A ROS 2 action SUT for the real-DDS path. rclpy is imported only when run.

The action is `action_tutorials_interfaces/Fibonacci` because it ships with every
ROS 2 install; `order` is read as a step count, `partial_sequence` as a state report.
Using a stock interface keeps the harness free of any generated message package,
which is the point: a black-box harness must not depend on the SUT's own IDL.

Modes mirror the mock SUT so the same scenario can run on either transport.
"""

from __future__ import annotations

import argparse
import time


class Ros2Sut:
    def __init__(self, args: argparse.Namespace) -> None:
        import rclpy
        from rclpy.action import ActionServer, CancelResponse, GoalResponse
        from rclpy.callback_groups import ReentrantCallbackGroup
        from rclpy.node import Node
        from action_tutorials_interfaces.action import Fibonacci
        from std_srvs.srv import Trigger
        from std_msgs.msg import String

        self.args = args
        self.time = time
        rclpy.init()
        self.rclpy = rclpy
        self.node = Node("assurance_sut")
        self.action_type = Fibonacci
        self.executor_server = ActionServer(
            self.node, Fibonacci, args.action, execute_callback=self.execute,
            goal_callback=lambda _g: GoalResponse.ACCEPT,
            cancel_callback=lambda _g: (CancelResponse.ACCEPT if args.cancel_policy == "honour"
                                       else CancelResponse.REJECT),
            callback_group=ReentrantCallbackGroup(),
        )
        self.held: str | None = None
        self.stop_ack_count = 0
        # A STOP service and a state topic: the observable half of I4/I5 on a real stack.
        self.stop_service = self.node.create_service(Trigger, "assurance_stop", self.stop)
        self.state_pub = self.node.create_publisher(String, "assurance_state", 10)
        self.publish_state("idle")
        self.node.get_logger().info(
            f"assurance SUT up: action={args.action} work_ms={args.work_ms} "
            f"drop_ack={args.drop_ack} delay_ack={args.delay_ack} stale_epoch={args.stale_epoch}")

    def publish_state(self, state: str) -> None:
        from std_msgs.msg import String
        self.state_pub.publish(String(data=state))

    def stop(self, _request, response):
        """I5: a STOP is only real when it is acknowledged and the state actually changes."""
        if self.args.cancel_policy == "ignore":
            response.success = False
            response.message = "stop ignored"
            return response
        self.held = None
        self.publish_state("idle")
        response.success = True
        response.message = "stopped"
        return response

    def execute(self, goal_handle):
        Fibonacci = self.action_type
        steps = max(1, int(goal_handle.request.order))
        resource = "arm"
        if self.held is not None and self.args.claim_policy == "deny":
            self.node.get_logger().info(f"claim denied for {resource}")
            goal_handle.abort()
            return Fibonacci.Result(sequence=[])
        self.held = resource
        self.publish_state("busy")
        feedback = Fibonacci.Feedback()
        for step in range(1, steps + 1):
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                self.held = None
                self.publish_state("idle")
                return Fibonacci.Result(sequence=[])
            self.time.sleep(self.args.work_ms / 1000.0)
            feedback.partial_sequence = [step]
            goal_handle.publish_feedback(feedback)
        if self.args.drop_ack:
            self.held = None
            # Keep the goal alive without ever completing it: the client must time out.
            while self.rclpy.ok() and not goal_handle.is_cancel_requested:
                self.time.sleep(0.05)
            goal_handle.canceled()
            return Fibonacci.Result(sequence=[])
        if self.args.delay_ack:
            self.time.sleep(self.args.delay_ack / 1000.0)
        self.held = None
        self.publish_state("idle")
        if self.args.stale_epoch:
            # Report a result that belongs to the previous goal: the classic late-ACK mixup.
            goal_handle.succeed()
            return Fibonacci.Result(sequence=[0])
        goal_handle.succeed()
        return Fibonacci.Result(sequence=list(range(1, steps + 1)))

    def spin(self) -> None:
        from rclpy.executors import MultiThreadedExecutor
        executor = MultiThreadedExecutor(num_threads=4)
        executor.add_node(self.node)
        try:
            executor.spin()
        except KeyboardInterrupt:
            pass
        finally:
            executor.shutdown()
            self.executor_server.destroy()
            self.node.destroy_node()
            self.rclpy.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", default="execute_task")
    ap.add_argument("--work-ms", type=int, default=40)
    ap.add_argument("--claim-policy", default="deny", choices=["deny", "grant-all"])
    ap.add_argument("--cancel-policy", default="honour", choices=["honour", "ignore"])
    ap.add_argument("--drop-ack", action="store_true")
    ap.add_argument("--delay-ack", type=int, default=0, metavar="MS")
    ap.add_argument("--stale-epoch", action="store_true")
    Ros2Sut(ap.parse_args()).spin()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
