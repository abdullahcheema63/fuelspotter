"""Step 4: where to buy fuel, and how much.

Minimises fuel cost + stop cost x number of stops, exactly. Fuel is counted in miles of range;
"empty" means down to the reserve. Works on plain numbers (mile markers and prices), nothing else.
"""
import math

import numpy as np
from django.conf import settings

from api.exceptions import TripImpossible
from api.services.stations import find_gap

TIE_BREAK_USD = 1e-6              # per stop: between equal-cost plans, prefer fewer stops
EPSILON = 1e-9


class StopSearch:
    """Exact search for the lowest (fuel cost + stop cost x number of stops).

    Why two numbers per station are enough. Take two stops in a row, A then B. If the truck
    leaves A not full and reaches B not empty, shifting fuel from the pricier of the two to the
    cheaper never costs more. Repeat, and every stop either FILLS UP or buys JUST ENOUGH to reach
    the next stop empty. So per station we keep:

        arrive_empty[k]   cheapest way to reach k with an empty tank
        leave_full[k]     cheapest way to drive away from k with a full tank

    Pseudocode (U = usable tank in miles, each purchase also pays the stop cost):

        leave_full[start] = 0
        for each station i, in order along the route:
            leave_full[i] = cheapest of
                arrive_empty[i] + price[i] * U                    # arrived empty, fill up
                leave_full[h]   + price[i] * dist(h, i)           # h = earlier fill-up in range
            for each k that i can reach:                          # buy just enough at i
                offer arrive_empty[k] = arrive_empty[i] + price[i] * dist(i, k)
            for each k up to one top-up beyond i's reach:         # leave i full, top up at j
                j = cheapest station that i can reach and that can reach k
                offer arrive_empty[k] = leave_full[i] + price[j] * (dist(i, k) - U)
        answer = arrive_empty[finish]

    The inner loops run as numpy operations over slices: about 4 ms for 200 stations.
    """

    START = 0

    def __init__(self, station_miles, station_prices, total_miles,
                 fuel_at_start, usable, stop_cost):
        self.count = len(station_miles)
        self.finish = self.count + 1
        self.usable = usable
        self.per_stop = stop_cost + TIE_BREAK_USD
        # Nodes along the road: 0 = start, 1..count = stations, count+1 = finish.
        # The start is "left full" at an imaginary point, so that fuel_at_start remains at mile 0.
        self.position = np.concatenate([[fuel_at_start - usable], station_miles, [total_miles]])
        per_mile = np.asarray(station_prices, dtype=float) / settings.VEHICLE_MILES_PER_GALLON
        self.price_per_mile = np.concatenate([[0.0], per_mile, [0.0]])

        nodes = self.count + 2
        self.arrive_empty = np.full(nodes, math.inf)
        self.leave_full = np.full(nodes, math.inf)
        self.leave_full[self.START] = 0.0
        # How each best value was reached, to rebuild the plan afterwards.
        self.empty_came_from = np.full(nodes, -1)       # node the truck drove from
        self.empty_bought_at = np.full(nodes, -1)       # station where that leg's fuel was bought
        self.full_came_from = np.full(nodes, -1)        # earlier fill-up, or itself (arrived empty)

        # For every node: the nodes within one tank behind it, one tank ahead, two tanks ahead.
        position = self.position
        one_tank = usable + EPSILON
        two_tanks_on = position + 2 * one_tank
        self.first_behind = np.searchsorted(position, position - one_tank, side='left')
        self.last_ahead = np.searchsorted(position, position + one_tank, side='right') - 1
        self.last_two_tanks_ahead = np.searchsorted(position, two_tanks_on, side='right') - 1

    def run(self):
        """Fill in the tables; returns [(station number, miles of fuel bought)] in trip order."""
        self.top_up_after(self.START)
        for station in range(1, self.count + 1):
            self.fill_up_at(station)
            self.buy_just_enough_at(station)
            self.top_up_after(station)
        return self.purchases()

    def fill_up_at(self, i):
        """leave_full[i]: refill after an earlier fill-up, or fill up after arriving empty."""
        earlier = slice(self.first_behind[i], i)
        miles_bought = self.position[i] - self.position[earlier]
        cost = self.leave_full[earlier] + self.price_per_mile[i] * miles_bought + self.per_stop
        cost[miles_bought <= EPSILON] = math.inf          # nothing to buy is not a stop
        if cost.size and cost.min() < self.leave_full[i]:
            best = int(cost.argmin())
            self.leave_full[i] = cost[best]
            self.full_came_from[i] = earlier.start + best

        a_full_tank = self.price_per_mile[i] * self.usable
        after_arriving_empty = self.arrive_empty[i] + a_full_tank + self.per_stop
        if after_arriving_empty < self.leave_full[i]:
            self.leave_full[i] = after_arriving_empty
            self.full_came_from[i] = i

    def buy_just_enough_at(self, i):
        """From arriving empty at i: buy exactly the fuel to reach each node within range."""
        if not math.isfinite(self.arrive_empty[i]):
            return
        targets = slice(i + 1, self.last_ahead[i] + 1)
        miles_bought = self.position[targets] - self.position[i]
        cost = self.arrive_empty[i] + self.price_per_mile[i] * miles_bought + self.per_stop
        self.offer_arrive_empty(targets, cost, came_from=i, bought_at=i)

    def top_up_after(self, i):
        """From leaving i full: top up at the cheapest station on the way, to reach further."""
        if not math.isfinite(self.leave_full[i]):
            return
        # Stations i can reach, and the nodes just beyond its reach.
        on_the_way = slice(i + 1, min(self.last_ahead[i], self.count) + 1)
        targets = slice(self.last_ahead[i] + 1, self.last_two_tanks_ahead[i] + 1)
        if on_the_way.stop <= on_the_way.start or targets.stop <= targets.start:
            return

        # A top-up station must be able to reach the target, so for each target the choice is
        # "the cheapest station from the first one in range of it, onwards".
        cheapest_price, cheapest_at = cheapest_from_each_onwards(self.price_per_mile[on_the_way])
        first_in_range = np.maximum(self.first_behind[targets], on_the_way.start) - on_the_way.start
        has_top_up = first_in_range < len(cheapest_price)
        first_in_range = np.minimum(first_in_range, len(cheapest_price) - 1)

        miles_bought = self.position[targets] - self.position[i] - self.usable
        cost = self.leave_full[i] + cheapest_price[first_in_range] * miles_bought + self.per_stop
        cost[~has_top_up] = math.inf
        bought_at = on_the_way.start + cheapest_at[first_in_range]
        self.offer_arrive_empty(targets, cost, came_from=i, bought_at=bought_at)

    def offer_arrive_empty(self, targets, cost, came_from, bought_at):
        """Keep each offered way of arriving empty that beats what the target already has."""
        better = cost < self.arrive_empty[targets]
        self.arrive_empty[targets][better] = cost[better]
        self.empty_came_from[targets][better] = came_from
        self.empty_bought_at[targets][better] = np.broadcast_to(bought_at, cost.shape)[better]

    def purchases(self):
        """Walk back from 'arrive at the finish empty' to list what was bought where."""
        if not math.isfinite(self.arrive_empty[self.finish]):
            return None
        bought = []
        node = self.finish
        arrived_empty = True
        while node != self.START:
            if arrived_empty:
                came_from = self.empty_came_from[node]
                station = self.empty_bought_at[node]
                if station == came_from:                  # bought just enough there
                    miles = self.position[node] - self.position[station]
                else:                                     # left `came_from` full, topped up here
                    miles = self.position[node] - self.position[came_from] - self.usable
                    arrived_empty = False
                bought.append((station, miles))
                node = came_from
            else:
                came_from = self.full_came_from[node]
                if came_from == node:                     # arrived empty and filled up
                    bought.append((node, self.usable))
                    arrived_empty = True
                else:                                     # refilled what was burned since then
                    bought.append((node, self.position[node] - self.position[came_from]))
                    node = came_from
        return [(int(station) - 1, float(miles)) for station, miles in reversed(bought)]


def cheapest_from_each_onwards(prices):
    """For each position: the lowest price from there to the end, and where it is.

    [3, 1, 2] -> prices [1, 1, 2], positions [1, 1, 2]. A running minimum taken from the right.
    """
    from_the_right = prices[::-1]
    lowest = np.minimum.accumulate(from_the_right)
    is_new_low = from_the_right <= lowest
    where = np.maximum.accumulate(np.where(is_new_low, np.arange(len(prices)), 0))
    return lowest[::-1], (len(prices) - 1 - where)[::-1]


def plan_stops(station_miles, station_prices, total_miles, start_fuel, reserve, stop_cost):
    """-> ([(station number, miles of fuel bought)], miles of fuel left). Raises TripImpossible."""
    vehicle_range = settings.VEHICLE_RANGE_MILES
    usable = vehicle_range - reserve
    fuel_at_start = min(start_fuel, vehicle_range) - reserve
    if usable <= 0 or fuel_at_start < 0:
        raise TripImpossible(0.0, 0.0, 0.0)
    gap = find_gap(station_miles, total_miles, start_fuel, reserve)
    if gap:
        raise TripImpossible(*gap)
    if total_miles <= fuel_at_start:                      # the starting tank covers the trip
        return [], fuel_at_start - total_miles + reserve

    search = StopSearch(station_miles, station_prices, total_miles,
                        fuel_at_start, usable, stop_cost)
    return search.run(), reserve                          # the plan arrives exactly at the reserve
