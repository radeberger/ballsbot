from math import pi
import json

from ballsbot.lidar import Lidar
from ballsbot.servos import get_controls
from ballsbot.utils import keep_rps, run_as_thread
from ballsbot.geometry import distance
from ballsbot.odometry import Odometry
from ballsbot.imu import IMU_Threaded
from ballsbot.tracking import TrackerLight
# from ballsbot.grid import Grid
from ballsbot_cpp import ballsbot_cpp


class Explorer:
    TURN_DIAMETER = 0.88
    CHECK_RADIUS = 2.
    A_BIT_CENTER_Y = CHECK_RADIUS / 2. * 2.5
    STOP_DISTANCE = 0.35
    FROM_LIDAR_TO_CENTER = 0.07
    FEAR_DISTANCE = 0.05
    CAR_WIDTH = 0.18
    HALF_CAR_WIDTH = CAR_WIDTH / 2
    STOP = {'steering': 0., 'throttle': 0.}
    FORWARD_THROTTLE = 0.5
    BACKWARD_TROTTLE = -0.5
    FORWARD_BRAKE = -0.4
    BACKWARD_BRAKE = 0.4
    RIGHT = 1.
    LEFT = -1.
    A_BIT_RIGHT = 0.5
    A_BIT_LEFT = -0.5
    INNER_OFFSET = 0.03

    def __init__(self, test_run=False, profile_mocks=None):
        if profile_mocks is None:
            self.lidar = Lidar(test_run)
        else:
            test_run = True
            self.lidar = profile_mocks['lidar']
        self.BODY_POSITION = self.lidar.calibration_to_xywh(self.lidar.calibration)
        self.test_run = test_run

        # self.grid = Grid()
        self.grid = ballsbot_cpp

        if not test_run:
            self.car_controls = get_controls()
            self.imu = IMU_Threaded()
            self.odometry = Odometry(self.imu, self.car_controls['throttle'])
            self.tracker = TrackerLight(self.imu, self.odometry)
        elif profile_mocks is not None:
            self.car_controls = profile_mocks['car_controls']
            self.imu = None
            self.odometry = profile_mocks['odometry']
            self.tracker = profile_mocks['tracker']

        self.track_info = []
        self.cached_speed = None
        self.cached_pose = None
        self.cached_points = None
        self.cached_direction = None

    def run(self, save_track_info=False):
        def tracker_run():
            self.tracker.start()

        if not self.test_run:
            run_as_thread(tracker_run)

        ts = None
        direction = self.STOP
        steps_with_direction = 0
        keep_for = 0
        while True:
            ts = keep_rps(ts, fps=4)
            prev_direction = direction
            if keep_for <= 0:
                direction, keep_for = self._get_next_move(direction, steps_with_direction)
                print(direction['steering'], direction['throttle'])
            keep_for -= 1
            # print('direction: {}, turn: {}, speed {:0.4f}'.format(
            #     direction['throttle'], direction['steering'], self.odometry.get_speed()
            # ))
            self._follow_direction(direction)

            if save_track_info:
                self.track_info.append({
                    'speed': self.cached_speed,
                    'pose': self.cached_pose,
                    'points': self.cached_points,
                    'direction': self.cached_direction,
                })

            if prev_direction == direction:
                steps_with_direction += 1
            else:
                steps_with_direction = 0

    def _can_move_straight_forward(self, nearby_points):
        min_y = self.BODY_POSITION['y'] - self.FEAR_DISTANCE
        max_y = self.BODY_POSITION['y'] + self.BODY_POSITION['h'] + self.FEAR_DISTANCE
        min_x = self.BODY_POSITION['x'] + self.BODY_POSITION['w']
        max_x = min_x + self.CHECK_RADIUS
        stop_x = min_x + self._get_stop_distance()
        min_x -= self.INNER_OFFSET

        nearest_x = max_x
        for a_point in nearby_points:
            if min_x < a_point[0] < max_x and min_y <= a_point[1] <= max_y:
                if a_point[0] < stop_x:
                    return False, 0.
                elif nearest_x > a_point[0]:
                    nearest_x = a_point[0]
        return True, nearest_x

    def _can_move_straight_backward(self, nearby_points):
        min_y = self.BODY_POSITION['y'] - self.FEAR_DISTANCE
        max_y = self.BODY_POSITION['y'] + self.BODY_POSITION['h'] + self.FEAR_DISTANCE
        max_x = self.BODY_POSITION['x']
        min_x = max_x - self.CHECK_RADIUS
        stop_x = max_x - self._get_stop_distance()
        max_x += self.INNER_OFFSET

        nearest_x = min_x
        for a_point in nearby_points:
            if min_x < a_point[0] < max_x and min_y <= a_point[1] <= max_y:
                if a_point[0] > stop_x:
                    return False, 0.
                elif nearest_x < a_point[0]:
                    nearest_x = a_point[0]
        return True, -nearest_x

    def _filter_right_points(self, nearby_points):
        left_center, right_center, column_radius = self._get_columns()
        outer_radius = column_radius + 2 * (self.HALF_CAR_WIDTH + self.FEAR_DISTANCE)

        def on_a_curve(a_point):
            return distance(right_center, a_point) < outer_radius

        return list(filter(on_a_curve, nearby_points))

    def _filter_left_points(self, nearby_points):
        left_center, right_center, column_radius = self._get_columns()
        outer_radius = column_radius + 2 * (self.HALF_CAR_WIDTH + self.FEAR_DISTANCE)

        def on_a_curve(a_point):
            return distance(left_center, a_point) < outer_radius

        return list(filter(on_a_curve, nearby_points))

    def _filter_a_bit_left_points(self, nearby_points):
        left_center = [0, self.A_BIT_CENTER_Y]
        center_radius = self.A_BIT_CENTER_Y
        outer_radius = center_radius + self.HALF_CAR_WIDTH + self.FEAR_DISTANCE
        inner_radius = center_radius - self.HALF_CAR_WIDTH - self.FEAR_DISTANCE

        def on_a_curve(a_point):
            return inner_radius < distance(left_center, a_point) < outer_radius

        return list(filter(on_a_curve, nearby_points))

    def _filter_a_bit_right_points(self, nearby_points):
        right_center = [0, -self.A_BIT_CENTER_Y]
        center_radius = self.A_BIT_CENTER_Y
        outer_radius = center_radius + self.HALF_CAR_WIDTH + self.FEAR_DISTANCE
        inner_radius = center_radius - self.HALF_CAR_WIDTH - self.FEAR_DISTANCE

        def on_a_curve(a_point):
            return inner_radius < distance(right_center, a_point) < outer_radius

        return list(filter(on_a_curve, nearby_points))

    def _can_move_some_forward(self, nearby_points, a_filter):
        nearby_points = a_filter(nearby_points)
        min_x = self.BODY_POSITION['x'] + self.BODY_POSITION['w']
        max_x = min_x + self.CHECK_RADIUS
        stop_x = min_x + self._get_stop_distance()
        min_x -= self.INNER_OFFSET

        nearest_x = max_x
        for a_point in nearby_points:
            if min_x < a_point[0]:
                if a_point[0] < stop_x:
                    return False, 0.
                elif nearest_x > a_point[0]:
                    nearest_x = a_point[0]
        return True, nearest_x

    def _can_move_right_forward(self, nearby_points):
        return self._can_move_some_forward(nearby_points, self._filter_right_points)

    def _can_move_left_forward(self, nearby_points):
        return self._can_move_some_forward(nearby_points, self._filter_left_points)

    def _can_move_a_bit_right_forward(self, nearby_points):
        return self._can_move_some_forward(nearby_points, self._filter_a_bit_right_points)

    def _can_move_a_bit_left_forward(self, nearby_points):
        return self._can_move_some_forward(nearby_points, self._filter_a_bit_left_points)

    def _can_move_some_backward(self, nearby_points, a_filter):
        nearby_points = a_filter(nearby_points)
        max_x = self.BODY_POSITION['x']
        min_x = max_x - self.CHECK_RADIUS
        stop_x = max_x - self._get_stop_distance()
        max_x += self.INNER_OFFSET

        nearest_x = min_x
        for a_point in nearby_points:
            if a_point[0] < max_x:
                if a_point[0] > stop_x:
                    return False, 0.
                elif nearest_x < a_point[0]:
                    nearest_x = a_point[0]
        return True, -nearest_x

    def _can_move_right_backward(self, nearby_points):
        return self._can_move_some_backward(nearby_points, self._filter_right_points)

    def _can_move_left_backward(self, nearby_points):
        return self._can_move_some_backward(nearby_points, self._filter_left_points)

    def _can_move_a_bit_right_backward(self, nearby_points):
        return self._can_move_some_backward(nearby_points, self._filter_a_bit_right_points)

    def _can_move_a_bit_left_backward(self, nearby_points):
        return self._can_move_some_backward(nearby_points, self._filter_a_bit_left_points)

    def _get_next_move(self, prev_direction, steps_with_direction):
        self.cached_direction = self.odometry.get_direction()
        if prev_direction['throttle'] == self.FORWARD_BRAKE or prev_direction['throttle'] == self.BACKWARD_BRAKE:
            if self.cached_direction > 0. and prev_direction['throttle'] == self.FORWARD_BRAKE \
                    or self.cached_direction < 0. and prev_direction['throttle'] == self.BACKWARD_BRAKE:
                return prev_direction, 1
            else:
                return self.STOP, 1
        elif prev_direction['throttle'] == self.FORWARD_THROTTLE or prev_direction['throttle'] == self.BACKWARD_TROTTLE:
            if self.cached_direction == 0. and steps_with_direction > 3:  # stop when jammed or on driver error
                return self.STOP, 4

        nearby_points = self._get_nearby_points()
        self.cached_pose = self.tracker.get_current_pose()
        self.grid.update_grid(self.lidar.get_lidar_points(), self.cached_pose)

        can_move = {
            (0., 1.): self._can_move_straight_forward(nearby_points),
            (self.RIGHT, 1.): self._can_move_right_forward(nearby_points),
            (self.LEFT, 1.): self._can_move_left_forward(nearby_points),
            (self.A_BIT_RIGHT, 1.): self._can_move_a_bit_right_forward(nearby_points),
            (self.A_BIT_LEFT, 1.): self._can_move_a_bit_left_forward(nearby_points),
            (0., -1.): self._can_move_straight_backward(nearby_points),
            (self.RIGHT, -1.): self._can_move_right_backward(nearby_points),
            (self.LEFT, -1.): self._can_move_left_backward(nearby_points),
            (self.A_BIT_RIGHT, -1.): self._can_move_a_bit_right_backward(nearby_points),
            (self.A_BIT_LEFT, -1.): self._can_move_a_bit_left_backward(nearby_points),
        }
        if prev_direction['throttle'] == self.FORWARD_THROTTLE \
                and len(list(filter(lambda x: x[0][1] == 1. and x[1][0], can_move.items()))) == 0:
            return {'steering': prev_direction['steering'], 'throttle': self.FORWARD_BRAKE}, 1
        elif prev_direction['throttle'] == self.BACKWARD_TROTTLE \
                and len(list(filter(lambda x: x[0][1] == -1. and x[1][0], can_move.items()))) == 0:
            return {'steering': prev_direction['steering'], 'throttle': self.BACKWARD_BRAKE}, 1
        elif len(list(filter(lambda x: x[0], can_move.values()))) == 0:
            return self.STOP, 1

        weights = self.grid.get_directions_weights(
            self.cached_pose, {'to_car_center': self.FROM_LIDAR_TO_CENTER, 'turn_radius': self.TURN_DIAMETER / 2.})

        for sector_key, value in can_move.items():
            if not value[0]:
                weights[sector_key] = 0.  # can't move
                continue

            if abs(value[1]) < 0.5:
                weights[sector_key] /= 2.  # more can move - greater weight

            if sector_key[1] > 0. and prev_direction['throttle'] == self.FORWARD_THROTTLE \
                    or sector_key[1] < 0. and prev_direction['throttle'] == self.BACKWARD_TROTTLE:
                weights[sector_key] *= 4.  # trying to keep prev direction

        weights = {k: v for k, v in filter(lambda x: x[1] > 0., weights.items())}
        if len(weights.keys()) > 0:
            print(weights)
            sector_key = list(sorted(weights.items(), key=lambda x: x[1]))[-1][0]
        else:  # fallback
            print('fallback')
            if prev_direction['throttle'] == self.BACKWARD_TROTTLE:
                filter_value = -1.
            else:
                filter_value = 1.

            can_move_filtered = list(filter(lambda x: x[1][0] and x[0][1] == filter_value, can_move.items()))
            if len(can_move_filtered) == 0:
                can_move_filtered = list(filter(lambda x: x[1][0], can_move.items()))

            sector_key = list(sorted(
                can_move_filtered,
                key=lambda y: (
                    y[1][1],  # max distance
                    -abs(y[0][0]),  # prefer straight
                )
            ))[-1][0]

        steering = sector_key[0]
        throttle = self.FORWARD_THROTTLE if sector_key[1] > 0. else self.BACKWARD_TROTTLE

        if prev_direction['throttle'] == self.FORWARD_THROTTLE and throttle == self.BACKWARD_TROTTLE:
            throttle = self.FORWARD_BRAKE
        elif prev_direction['throttle'] == self.BACKWARD_TROTTLE and throttle == self.FORWARD_THROTTLE:
            throttle = self.BACKWARD_BRAKE

        return {'steering': steering, 'throttle': throttle}, 1

    def _get_columns(self):
        return [0., self.TURN_DIAMETER], [0., -self.TURN_DIAMETER], \
               self.TURN_DIAMETER - self.HALF_CAR_WIDTH - self.FEAR_DISTANCE

    def _get_nearby_points(self):
        range_limit = self.CHECK_RADIUS + self.HALF_CAR_WIDTH + self.FEAR_DISTANCE
        range_limit += abs(self.FROM_LIDAR_TO_CENTER)
        nearby_points = self.lidar.get_radial_lidar_points(range_limit, cached=False)
        self.cached_points = self.lidar.get_radial_lidar_points()  # cached, no limit

        nearby_points = list(filter(self._ellipse_like_range_filter, nearby_points))

        nearby_points = self.lidar.radial_points_to_cartesian(nearby_points)
        left_center, right_center, column_radius = self._get_columns()

        def not_in_columns_and_a_car(a_point):
            if a_point[1] > 0.:
                if distance(left_center, a_point) < column_radius:
                    return False
                elif a_point[1] > left_center[1] \
                        and left_center[0] - column_radius < a_point[0] < left_center[0] + column_radius:
                    return False
            elif a_point[1] < 0.:
                if distance(right_center, a_point) < column_radius:
                    return False
                elif a_point[1] < right_center[1] \
                        and right_center[0] - column_radius < a_point[0] < right_center[0] + column_radius:
                    return False

            if self.BODY_POSITION['x'] + self.INNER_OFFSET <= a_point[0] <= \
                    self.BODY_POSITION['x'] + self.BODY_POSITION['w'] - self.INNER_OFFSET \
                    and self.BODY_POSITION['y'] + self.INNER_OFFSET <= a_point[1] <= \
                    self.BODY_POSITION['y'] + self.BODY_POSITION['h'] - self.INNER_OFFSET:
                return False

            return True

        return list(filter(not_in_columns_and_a_car, nearby_points))

    def _ellipse_like_range_filter(self, a_point):
        angle = a_point['angle']
        angle %= 2 * pi
        if angle < 0:
            angle = -angle

        if pi / 2 <= angle <= 3 * pi / 2:
            lidar_to_center = 0.
        else:
            lidar_to_center = self.FROM_LIDAR_TO_CENTER

        if angle >= pi:
            angle -= pi
        if angle > pi / 2:
            angle = pi - angle

        range_limit = self.CHECK_RADIUS + self.FEAR_DISTANCE \
                      + self.HALF_CAR_WIDTH * angle + lidar_to_center * (pi / 2 - angle)
        return a_point['distance'] < range_limit

    def _follow_direction(self, direction):
        self.car_controls['steering'].run(direction['steering'])
        self.car_controls['throttle'].run(direction['throttle'])

    def _get_stop_distance(self):
        self.cached_speed = self.odometry.get_speed()
        if self.cached_speed > 0.5:
            return self.STOP_DISTANCE * self.cached_speed / 0.5
        else:
            return self.STOP_DISTANCE

    def track_info_to_a_file(self, file_path):
        with open(file_path, 'w') as a_file:
            a_file.write(json.dumps(self.track_info))
