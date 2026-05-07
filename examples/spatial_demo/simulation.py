"""
LLM-based agent in 2D worlds with multiple places.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import time
import yaml
import logging
from typing import List, Tuple, Dict, Set, Optional
import numpy as np
from agent import Agent
from llm_backends import create_llm_client
from utils import get_place_at_position, PlaceConfig

logger = logging.getLogger(__name__)

# Constants
MAX_POSITION_ATTEMPTS = 1000
LOG_INTERVAL = 10


class Simulation:
    """Main simulation class for LLM-based agent in 2D worlds with multiple places."""
    
    def __init__(self, config_path: str = "examples/spatial_demo/configs/config.yaml", output_dir: Optional[str] = None):
        """Initialize simulation from config file"""
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        # Output directory for logs
        self.output_dir = output_dir

        # Simulation parameters
        sim_config = self.config['simulation']
        self.duration = sim_config['duration']
        self.half_space_size = sim_config['half_space_size']
        self.half_place_size = sim_config.get('half_place_size', 5)
        self.initial_positions = sim_config.get("initial_positions")
        
        # Agent parameters
        agent_config = self.config['agents']
        self.num_agents = agent_config['num_agents']
        self.communication_radius = agent_config['communication_radius']
        self.memory_limit = agent_config.get('memory_limit', 20)
        self.memory_size = agent_config.get('memory_size', 5)
        self.message_history_limit = agent_config.get('message_history_limit', 10)
        self.message_context_size = agent_config.get('message_context_size', 3)
        self.personas = agent_config.get('personas', [])
        self.world_context = agent_config.get('world_context', '')
        self.genders = agent_config.get('genders', [])
        
        # Place parameters - support multiple places
        if 'places' not in self.config:
            raise ValueError("No 'places' configuration found in config file. Please use 'places:' key.")
        
        self.places = self.config['places']
        
        # Validate places configuration
        if not isinstance(self.places, list):
            raise ValueError("'places' must be a list of place configurations.")
        
        if len(self.places) == 0:
            raise ValueError("At least one place must be configured in 'places'.")
        
        # Validate each place configuration
        required_fields = ['name', 'type', 'center_x', 'center_y', 'half_size', 'capacity']
        for i, place in enumerate(self.places):
            if not isinstance(place, dict):
                raise ValueError(f"Place at index {i} must be a dictionary.")
            
            for field in required_fields:
                if field not in place:
                    raise ValueError(f"Place at index {i} is missing required field: '{field}'")
        
        place_names = [place['name'] for place in self.places]
        place_types = [place['type'] for place in self.places]
        logger.info(f"Initialized {len(self.places)} place(s): {place_names} (types: {place_types})")
        
        # Fire parameters (multiple fires supported)
        fires_config = self.config.get('fires', [])
        self.fire_configs: List[Dict] = []
        for i, fc in enumerate(fires_config):
            config_entry = {
                'name': fc.get('name', f'fire_{i}'),
                'start_step': fc['start_step'],
                'intensity': fc['intensity'],
                'radius': fc['radius'],
            }
            if 'center_x' in fc and 'center_y' in fc:
                config_entry['center_x'] = fc['center_x']
                config_entry['center_y'] = fc['center_y']
            self.fire_configs.append(config_entry)
            pos_info = f"({fc['center_x']}, {fc['center_y']})" if 'center_x' in fc else "random"
            logger.info(
                f"Fire '{config_entry['name']}' configured: step={fc['start_step']}, "
                f"intensity={fc['intensity']}, radius={fc['radius']}, position={pos_info}"
            )
        self.fire_states: List[Dict] = []  # Active fires

        # Economy parameters (optional)
        economy_config = self.config.get('economy', {})
        self.economy_enabled = economy_config.get('enabled', False)
        self.structure_credit_name = economy_config.get('structure_credit_name', 'structure credits')
        self.base_wage = float(economy_config.get('base_wage', 0.0))
        self.living_cost = float(economy_config.get('living_cost', 0.0))
        self.initial_cash_balance = float(economy_config.get('initial_cash_balance', 0.0))
        self.initial_structure_credits = float(economy_config.get('initial_structure_credits', 0.0))
        self.crowding_threshold = float(economy_config.get('crowding_threshold', 0.85))
        self.message_reward = float(economy_config.get('message_reward', 0.0))
        self.fire_coordination_reward = float(economy_config.get('fire_coordination_reward', 0.0))
        self.decongestion_reward = float(economy_config.get('decongestion_reward', 0.0))
        self.fire_escape_reward = float(economy_config.get('fire_escape_reward', 0.0))

        automation_config = economy_config.get('llm_automation', {})
        self.automation_enabled = self.economy_enabled and automation_config.get('enabled', False)
        self.automation_start_step = int(automation_config.get('start_step', self.duration + 1))
        self.automation_interval = max(1, int(automation_config.get('interval', 1)))
        self.job_losses_per_wave = max(0, int(automation_config.get('job_losses_per_wave', 0)))
        self.job_types = economy_config.get('job_types', self._default_job_types())
        self.recent_job_losses: List[Dict] = []
        self.total_job_losses = 0
        self.structure_credits_issued_this_step = 0.0

        if self.economy_enabled:
            self._validate_job_types()
            logger.info(
                "Economy enabled: wages=%.2f, living_cost=%.2f, automation=%s",
                self.base_wage,
                self.living_cost,
                self.automation_enabled
            )

        # LLM parameters
        llm_config = self.config['llm']
        self.llm_provider = llm_config.get('provider', 'ollama').lower()
        self.llm_client = create_llm_client(llm_config)
        self.llm_parallelism = max(1, int(llm_config.get('parallelism', 1)))
        self.llm_target = llm_config.get('model')
        if not self.llm_target:
            if self.llm_provider in {'command', 'cli'}:
                command = llm_config.get('command', [])
                if isinstance(command, list):
                    self.llm_target = " ".join(command)
                else:
                    self.llm_target = str(command)
            else:
                self.llm_target = self.llm_provider
        
        # Initialize agents
        self.agents: List[Agent] = []
        self.step = 0
        self.history: List[Dict] = []
        
        # Statistics - track per place
        self.stats = {
            'place_occupancy': [],  # Overall occupancy (all places combined)
            'agents_in_place': [],  # Total agents in any place
            'agents_outside_place': [],
            'communication_events': [],
            'places': {place['name']: {
                'occupancy': [],
                'agents_in_place': []
            } for place in self.places},
            'agents_in_fire_radius': [],  # Total agents in any fire radius
        }

        if self.economy_enabled:
            self.stats.update({
                'employed_agents': [],
                'unemployed_agents': [],
                'average_cash_balance': [],
                'average_structure_credit_balance': [],
                'cumulative_job_losses': [],
                'structure_credits_issued': [],
            })

    def _run_in_parallel(self, items: List, worker):
        """Run a pure worker across items, preserving order."""
        if self.llm_parallelism <= 1 or len(items) <= 1:
            return [worker(item) for item in items]

        max_workers = min(self.llm_parallelism, len(items))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(worker, items))

    def _is_position_in_place(self, position: Tuple[int, int]) -> bool:
        """Check if a position is inside any place"""
        return get_place_at_position(position, self.places) is not None

    def _default_job_types(self) -> List[Dict]:
        """Default jobs with different exposure to LLM automation."""
        return [
            {"name": "routine_clerk", "automation_risk": 0.95},
            {"name": "customer_support", "automation_risk": 0.85},
            {"name": "content_writer", "automation_risk": 0.80},
            {"name": "accounting_assistant", "automation_risk": 0.75},
            {"name": "teacher", "automation_risk": 0.40},
            {"name": "care_worker", "automation_risk": 0.20},
        ]

    def _validate_job_types(self) -> None:
        """Validate configured job types for the economy layer."""
        if not isinstance(self.job_types, list) or not self.job_types:
            raise ValueError("'economy.job_types' must be a non-empty list.")

        for i, job in enumerate(self.job_types):
            if not isinstance(job, dict):
                raise ValueError(f"Job type at index {i} must be a dictionary.")
            if 'name' not in job or 'automation_risk' not in job:
                raise ValueError(
                    f"Job type at index {i} must include 'name' and 'automation_risk'."
                )

    def _sample_job_type(self) -> Dict:
        """Sample one job profile for a new agent."""
        return random.choice(self.job_types)

    def _log_economy_event(self, event_type: str, payload: Dict) -> None:
        """Log economy-related events to a jsonl file."""
        if not self.output_dir:
            return

        os.makedirs(self.output_dir, exist_ok=True)
        economy_file = os.path.join(self.output_dir, "economy_events.jsonl")
        record = {
            "step": self.step,
            "event_type": event_type,
            **payload,
        }
        with open(economy_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _is_agent_in_any_fire(self, agent: Agent) -> bool:
        """Check whether the agent is inside the radius of any active fire."""
        for fire in self.fire_states:
            if fire.get('active') and agent.distance_to(fire['position']) <= fire['radius']:
                return True
        return False

    def _award_structure_credits(self, agent: Agent, amount: float, reason: str) -> None:
        """Award structure-sustain currency and log the reason."""
        if not self.economy_enabled or amount <= 0:
            return

        agent.receive_structure_credits(amount)
        self.structure_credits_issued_this_step += amount
        self._log_economy_event(
            "structure_credit",
            {
                "agent_id": agent.id,
                "amount": amount,
                "reason": reason,
            }
        )

    def _process_economy_opening(self) -> None:
        """Apply automation, wages, and living costs at the start of each step."""
        self.recent_job_losses = []
        self.structure_credits_issued_this_step = 0.0

        for agent in self.agents:
            agent.start_new_step_economy()

        if not self.economy_enabled:
            return

        should_run_automation = (
            self.automation_enabled
            and self.step >= self.automation_start_step
            and (self.step - self.automation_start_step) % self.automation_interval == 0
        )

        if should_run_automation:
            available_agents = [agent for agent in self.agents if agent.employed]
            layoffs_to_apply = min(self.job_losses_per_wave, len(available_agents))

            for _ in range(layoffs_to_apply):
                total_weight = sum(max(agent.automation_risk, 0.01) for agent in available_agents)
                pick = random.random() * total_weight
                cumulative = 0.0
                selected = available_agents[-1]
                for agent in available_agents:
                    cumulative += max(agent.automation_risk, 0.01)
                    if pick <= cumulative:
                        selected = agent
                        break

                selected.lose_job(self.step)
                available_agents.remove(selected)
                self.total_job_losses += 1

                event = {
                    "agent_id": selected.id,
                    "job_name": selected.job_name,
                    "automation_risk": selected.automation_risk,
                }
                self.recent_job_losses.append(event)
                self._log_economy_event("job_loss", event)
                logger.info(
                    "Step %s: Agent %s lost job '%s' due to LLM automation "
                    "(risk=%.2f)",
                    self.step,
                    selected.id,
                    selected.job_name,
                    selected.automation_risk
                )

        for agent in self.agents:
            if agent.employed:
                agent.receive_wage(self.base_wage)
            agent.pay_living_cost(self.living_cost)

    def _get_message_reward(
        self,
        agent: Agent,
        message_content: str,
        nearby_agents: List[Agent],
        place_status: Optional[Dict],
        fire_info: Optional[List[Dict]]
    ) -> float:
        """Calculate structure credits for messaging under risky conditions."""
        if not self.economy_enabled or not message_content or not nearby_agents:
            return 0.0

        reward = self.message_reward
        if fire_info:
            reward += self.fire_coordination_reward
        if place_status and place_status.get('occupancy_rate', 0.0) >= self.crowding_threshold:
            reward += self.decongestion_reward
        return reward

    def get_economy_status(self) -> Dict:
        """Return current economy status for prompts and visualization."""
        if not self.economy_enabled:
            return {"enabled": False}

        employed_agents = sum(1 for agent in self.agents if agent.employed)
        average_cash = (
            float(np.mean([agent.cash_balance for agent in self.agents]))
            if self.agents else 0.0
        )
        average_structure = (
            float(np.mean([agent.structure_credit_balance for agent in self.agents]))
            if self.agents else 0.0
        )

        return {
            "enabled": True,
            "structure_credit_name": self.structure_credit_name,
            "employed_agents": employed_agents,
            "unemployed_agents": self.num_agents - employed_agents,
            "unemployment_rate": (self.num_agents - employed_agents) / self.num_agents
            if self.num_agents else 0.0,
            "average_cash_balance": average_cash,
            "average_structure_credit_balance": average_structure,
            "recent_job_losses": list(self.recent_job_losses),
        }

    def _append_spatial_snapshot(self, overall_status: Dict) -> None:
        """Append one JSON line per step for habitat UI bridge (scripts/spatial_to_habitat.py)."""
        if not self.output_dir:
            return
        snapshot = {
            "step": self.step,
            "agents": [
                {
                    "id": agent.id,
                    "x": agent.position[0],
                    "y": agent.position[1],
                    "current_place": agent.current_place or "",
                    "in_place": bool(agent.in_place),
                }
                for agent in self.agents
            ],
            "places": overall_status.get("places", {}),
        }
        path = os.path.join(self.output_dir, "spatial_step_snapshots.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    def _log_message(
        self,
        from_agent_id: int,
        to_agent_id: int,
        message: str,
        reasoning: str = ""
    ) -> None:
        """Log a message to messages.jsonl file"""
        if not self.output_dir:
            return

        # Ensure output directory exists
        os.makedirs(self.output_dir, exist_ok=True)

        messages_file = os.path.join(self.output_dir, "messages.jsonl")
        record = {
            "step": self.step,
            "from": from_agent_id,
            "to": to_agent_id,
            "message": message,
            "reasoning": reasoning
        }

        with open(messages_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _log_memory_reasoning_batch(
        self,
        records: List[Dict]
    ) -> None:
        """Log memory and reasoning records in batch to memory_reasoning.jsonl file
        
        This is more efficient than writing one record at a time, especially
        when logging for all agents in each step.
        """
        if not self.output_dir or not records:
            return

        # Ensure output directory exists
        os.makedirs(self.output_dir, exist_ok=True)

        memory_reasoning_file = os.path.join(self.output_dir, "memory_reasoning.jsonl")
        
        # Write all records at once (buffered I/O)
        with open(memory_reasoning_file, 'a', encoding='utf-8') as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _generate_random_position(self) -> Tuple[int, int]:
        """Generate a random position within the space (origin-centered coordinate system)"""
        return (
            random.randint(-self.half_space_size, self.half_space_size),
            random.randint(-self.half_space_size, self.half_space_size)
        )
    
    def _generate_initial_positions(self, avoid_places: bool = True) -> List[Tuple[int, int]]:
        """Generate initial positions for agents"""
        if self.initial_positions is not None:
            if not isinstance(self.initial_positions, list):
                raise ValueError("'simulation.initial_positions' must be a list of [x, y] pairs.")
            if len(self.initial_positions) != self.num_agents:
                raise ValueError(
                    "'simulation.initial_positions' length must equal 'agents.num_agents'. "
                    f"Got {len(self.initial_positions)} vs {self.num_agents}."
                )
            positions: List[Tuple[int, int]] = []
            for i, raw in enumerate(self.initial_positions):
                if (
                    not isinstance(raw, (list, tuple))
                    or len(raw) != 2
                    or not isinstance(raw[0], (int, float))
                    or not isinstance(raw[1], (int, float))
                ):
                    raise ValueError(
                        f"'simulation.initial_positions[{i}]' must be [x, y] numbers. Got: {raw!r}"
                    )
                x = int(raw[0])
                y = int(raw[1])
                if not (-self.half_space_size <= x <= self.half_space_size) or not (-self.half_space_size <= y <= self.half_space_size):
                    raise ValueError(
                        f"'simulation.initial_positions[{i}]'={raw!r} is out of bounds "
                        f"for half_space_size={self.half_space_size}."
                    )
                if avoid_places and self._is_position_in_place((x, y)):
                    raise ValueError(
                        f"'simulation.initial_positions[{i}]'={(x, y)} is inside a place, "
                        "but avoid_places=True. Provide positions outside places or disable avoid_places."
                    )
                positions.append((x, y))
            return positions

        positions: List[Tuple[int, int]] = []
        used_positions: Set[Tuple[int, int]] = set()
        attempts = 0
        
        while len(positions) < self.num_agents and attempts < MAX_POSITION_ATTEMPTS:
            position = self._generate_random_position()
            
            # Skip if position is already used
            if position in used_positions:
                attempts += 1
                continue
            
            # Skip if position is in any place and we want to avoid it
            if avoid_places and self._is_position_in_place(position):
                attempts += 1
                continue
            
            positions.append(position)
            used_positions.add(position)
            attempts += 1
        
        # If we couldn't generate enough positions avoiding places, fill remaining
        if len(positions) < self.num_agents:
            logger.warning(
                f"Could only generate {len(positions)} unique positions avoiding places. "
                "Using all available space."
            )
            while len(positions) < self.num_agents:
                position = self._generate_random_position()
                if position not in used_positions:
                    positions.append(position)
                    used_positions.add(position)
        
        return positions
    
    def initialize_agents(self):
        """Initialize agents at random positions"""
        logger.info(f"Initializing {self.num_agents} agents...")
        
        positions = self._generate_initial_positions(avoid_places=True)
        
        # Create agents
        for i in range(self.num_agents):
            if i < len(self.genders):
                gender = str(self.genders[i])
            else:
                gender = random.choice(["male", "female"])
            persona = str(self.personas[i]) if i < len(self.personas) else ""
            job_type = self._sample_job_type() if self.economy_enabled else {
                "name": "service_worker",
                "automation_risk": 0.5
            }
            agent = Agent(
                agent_id=i,
                initial_position=positions[i],
                llm_client=self.llm_client,
                communication_radius=self.communication_radius,
                half_space_size=self.half_space_size,
                places=self.places,
                num_agents=self.num_agents,
                gender=gender,
                job_name=job_type['name'],
                automation_risk=float(job_type['automation_risk']),
                initial_cash_balance=self.initial_cash_balance,
                initial_structure_credits=self.initial_structure_credits,
                memory_limit=self.memory_limit,
                memory_size=self.memory_size,
                message_history_limit=self.message_history_limit,
                message_context_size=self.message_context_size,
                persona=persona,
                world_context=self.world_context,
            )
            agent.update_state()  # Initialize in_place state
            self.agents.append(agent)
        
        logger.info("Agents initialized successfully")
    
    def get_agents_in_place(self, place_name: Optional[str] = None) -> List[Agent]:
        """Get list of agents currently in a specific place or any place"""
        if place_name:
            return [agent for agent in self.agents if agent.current_place == place_name]
        return [agent for agent in self.agents if agent.in_place]
    
    def get_place_status(self, place_name: Optional[str] = None) -> Dict:
        """Get current place status for a specific place or overall status"""
        if place_name:
            # Get status for a specific place
            place_config = next((p for p in self.places if p['name'] == place_name), None)
            if not place_config:
                raise ValueError(f"Place '{place_name}' not found")
            
            agents_in_place = len(self.get_agents_in_place(place_name))
            capacity = place_config['capacity']
            occupancy_rate = agents_in_place / capacity

            return {
                "place_name": place_name,
                "agents_in_place": agents_in_place,
                "capacity": capacity,
                "occupancy_rate": occupancy_rate,
            }
        else:
            # Get overall status (all places combined)
            agents_in_place = len(self.get_agents_in_place())
            occupancy_rate = agents_in_place / self.num_agents
            
            # Get per-place status (optimized: calculate directly instead of recursive calls)
            place_statuses = {}
            for place in self.places:
                place_agents = len(self.get_agents_in_place(place['name']))
                place_capacity = place['capacity']
                place_occupancy_rate = place_agents / place_capacity

                place_statuses[place['name']] = {
                    "place_name": place['name'],
                    "agents_in_place": place_agents,
                    "capacity": place_capacity,
                    "occupancy_rate": place_occupancy_rate,
                }
            
            return {
                "agents_in_place": agents_in_place,
                "occupancy_rate": occupancy_rate,
                "places": place_statuses
            }
    
    def get_fire_info_for_agent(self, agent: Agent) -> Optional[List[Dict]]:
        """Return list of perceived fire info dicts, or None if no fires perceived.

        Implements Model B: only agents within each fire's radius get that fire's data.
        Agents outside all radii must learn about fires through messages.
        """
        if not self.fire_states:
            return None

        perceived = []
        for fire in self.fire_states:
            if not fire.get('active'):
                continue
            fire_pos = fire['position']
            distance = agent.distance_to(fire_pos)
            if distance <= fire['radius']:
                perceived.append({
                    'name': fire['name'],
                    'fire_position': fire_pos,
                    'intensity': fire['intensity'],
                    'radius': fire['radius'],
                    'agent_distance': round(distance, 2),
                })
        return perceived if perceived else None

    def step_simulation(self):
        """Execute one simulation step

        New order:
        1. All agents decide messages (without position information)
        2. Messages are sent to nearby agents (using decision-time positions)
        3. All agents decide actions (with position information and message content)
        4. Agents move to new positions
        """
        self.step += 1
        self._process_economy_opening()

        # Fire activation check (multiple fires)
        active_names = {f['name'] for f in self.fire_states}
        for fc in self.fire_configs:
            if fc['name'] not in active_names and self.step >= fc['start_step']:
                if 'center_x' in fc and 'center_y' in fc:
                    fire_pos = (fc['center_x'], fc['center_y'])
                else:
                    fire_pos = self._generate_random_position()
                fire_state = {
                    'name': fc['name'],
                    'position': fire_pos,
                    'intensity': fc['intensity'],
                    'radius': fc['radius'],
                    'start_step': fc['start_step'],
                    'active': True,
                }
                self.fire_states.append(fire_state)
                logger.info(
                    f"FIRE '{fc['name']}' started at position {fire_pos} with intensity "
                    f"{fc['intensity']}, radius {fc['radius']}"
                )

        # Update agent states
        for agent in self.agents:
            agent.update_state(self.places)

        economy_context = self.get_economy_status()
        previous_fire_exposure = {
            agent.id: self._is_agent_in_any_fire(agent)
            for agent in self.agents
        }
        previous_places = {
            agent.id: agent.current_place
            for agent in self.agents
        }
        previous_place_occupancy = {
            place['name']: self.get_place_status(place['name'])['occupancy_rate']
            for place in self.places
        }

        # Phase 1: Collect message decisions from all agents (without position information)
        message_phase_started = time.perf_counter()
        logger.info(
            "Step %s: starting message decisions for %s agents (parallelism=%s)",
            self.step,
            len(self.agents),
            self.llm_parallelism,
        )
        message_inputs = []
        for agent in self.agents:
            nearby_agents = agent.get_nearby_agents(self.agents)
            # Get place status for the place the agent is in (or None if outside)
            agent_place_status = None
            if agent.in_place and agent.current_place:
                agent_place_status = self.get_place_status(agent.current_place)
            fire_info = self.get_fire_info_for_agent(agent)
            message_inputs.append(
                (agent, nearby_agents, agent_place_status, fire_info)
            )

        def collect_message_decision(item):
            agent, nearby_agents, agent_place_status, fire_info = item
            message_decision = agent.decide_message(
                agent_place_status,
                nearby_agents,
                self.step,
                fire_info=fire_info,
                economy_context=economy_context
            )
            return (
                agent,
                message_decision,
                nearby_agents,
                agent_place_status,
                fire_info,
            )

        message_decisions = self._run_in_parallel(message_inputs, collect_message_decision)
        logger.info(
            "Step %s: completed message decisions in %.2fs",
            self.step,
            time.perf_counter() - message_phase_started,
        )

        # Phase 2: Send messages (using decision-time nearby agents, before movement)
        for agent, message_decision, nearby_agents, agent_place_status, fire_info in message_decisions:
            message_content = message_decision.get('message', '')
            if message_content and nearby_agents:
                logger.info(
                    f"Step {self.step}: Agent {agent.id} sends message to {len(nearby_agents)} nearby agent(s): "
                    f"\"{message_content}\""
                )
                for other_agent in nearby_agents:
                    other_agent.receive_message(agent.id, message_content, step=self.step)
                    # Log message to jsonl file
                    self._log_message(
                        from_agent_id=agent.id,
                        to_agent_id=other_agent.id,
                        message=message_content,
                        reasoning=message_decision.get('reasoning', '')
                    )
                reward = self._get_message_reward(
                    agent,
                    message_content,
                    nearby_agents,
                    agent_place_status,
                    fire_info
                )
                self._award_structure_credits(agent, reward, "coordination_message")

        economy_context = self.get_economy_status()

        # Phase 3: Collect action decisions from all agents (with position information and message content)
        action_phase_started = time.perf_counter()
        logger.info(
            "Step %s: starting action decisions for %s agents (parallelism=%s)",
            self.step,
            len(message_decisions),
            self.llm_parallelism,
        )
        action_inputs = []
        for agent, message_decision, nearby_agents, _, _ in message_decisions:
            agent_place_status = None
            if agent.in_place and agent.current_place:
                agent_place_status = self.get_place_status(agent.current_place)
            message_content = message_decision.get('message', '')
            fire_info = self.get_fire_info_for_agent(agent)
            action_inputs.append(
                (agent, nearby_agents, agent_place_status, message_content, fire_info)
            )

        def collect_action_decision(item):
            agent, nearby_agents, agent_place_status, message_content, fire_info = item
            action_decision = agent.decide_action(
                agent_place_status,
                nearby_agents,
                self.step,
                message_content,
                fire_info=fire_info,
                economy_context=economy_context
            )
            memory_reasoning_record = {
                "step": self.step,
                "id": agent.id,
                "memory": action_decision.get('memory', ''),
                "reasoning": action_decision.get('reasoning', '')
            }
            return (agent, action_decision, memory_reasoning_record)

        action_results = self._run_in_parallel(action_inputs, collect_action_decision)
        action_decisions = []
        memory_reasoning_records = []  # Batch records for efficient I/O
        for agent, action_decision, memory_reasoning_record in action_results:
            action_decisions.append((agent, action_decision))
            memory_reasoning_records.append(memory_reasoning_record)
        logger.info(
            "Step %s: completed action decisions in %.2fs",
            self.step,
            time.perf_counter() - action_phase_started,
        )

        # Write all memory/reasoning records in batch (more efficient than individual writes)
        self._log_memory_reasoning_batch(memory_reasoning_records)

        # Phase 4: Execute movement (after messages are sent and actions are decided)
        for agent, action_decision in action_decisions:
            if action_decision['action'] == 'move' and action_decision['direction']:
                agent.move(action_decision['direction'])

        # Update states after movement
        for agent in self.agents:
            agent.update_state(self.places)

        if self.economy_enabled:
            for agent in self.agents:
                was_in_fire = previous_fire_exposure.get(agent.id, False)
                is_in_fire = self._is_agent_in_any_fire(agent)
                previous_place = previous_places.get(agent.id)

                if was_in_fire and not is_in_fire:
                    self._award_structure_credits(agent, self.fire_escape_reward, "left_fire_radius")

                if (
                    previous_place
                    and previous_place != agent.current_place
                    and previous_place_occupancy.get(previous_place, 0.0) >= self.crowding_threshold
                ):
                    self._award_structure_credits(agent, self.decongestion_reward, "left_overcrowded_place")

        # Record statistics
        agents_in_place = len(self.get_agents_in_place())
        overall_status = self.get_place_status()
        self.stats['place_occupancy'].append(overall_status['occupancy_rate'])
        self.stats['agents_in_place'].append(agents_in_place)
        self.stats['agents_outside_place'].append(self.num_agents - agents_in_place)
        
        # Record per-place statistics
        for place in self.places:
            place_status = self.get_place_status(place['name'])
            self.stats['places'][place['name']]['occupancy'].append(place_status['occupancy_rate'])
            self.stats['places'][place['name']]['agents_in_place'].append(place_status['agents_in_place'])
        
        # Record fire statistics (count agents in any active fire radius)
        if self.fire_states:
            agents_in_any_fire = set()
            for fire in self.fire_states:
                if fire.get('active'):
                    for agent in self.agents:
                        if agent.distance_to(fire['position']) <= fire['radius']:
                            agents_in_any_fire.add(agent.id)
            self.stats['agents_in_fire_radius'].append(len(agents_in_any_fire))
        else:
            self.stats['agents_in_fire_radius'].append(0)

        if self.economy_enabled:
            economy_status = self.get_economy_status()
            self.stats['employed_agents'].append(economy_status['employed_agents'])
            self.stats['unemployed_agents'].append(economy_status['unemployed_agents'])
            self.stats['average_cash_balance'].append(economy_status['average_cash_balance'])
            self.stats['average_structure_credit_balance'].append(
                economy_status['average_structure_credit_balance']
            )
            self.stats['cumulative_job_losses'].append(self.total_job_losses)
            self.stats['structure_credits_issued'].append(self.structure_credits_issued_this_step)

        # Store history
        history_entry = {
            'step': self.step,
            'place_status': overall_status,
            'agent_positions': [agent.position for agent in self.agents],
            'agents_in_place': [agent.id for agent in self.get_agents_in_place()],
            'fire_states': list(self.fire_states),
        }
        if self.economy_enabled:
            history_entry['economy'] = self.get_economy_status()
        self.history.append(history_entry)
        self._append_spatial_snapshot(overall_status)

        if self.step % LOG_INTERVAL == 0:
            place_info = ", ".join([
                f"{place['name']}: {self.get_place_status(place['name'])['agents_in_place']}"
                for place in self.places
            ])
            logger.info(
                f"Step {self.step}/{self.duration}: "
                f"{agents_in_place} agents in places ({place_info}), "
                f"{overall_status['occupancy_rate']:.1%} overall occupancy"
            )
            if self.economy_enabled:
                economy_status = self.get_economy_status()
                logger.info(
                    "Economy: employed=%s, unemployed=%s, avg cash=%.2f, "
                    "avg %s=%.2f, total job losses=%s",
                    economy_status['employed_agents'],
                    economy_status['unemployed_agents'],
                    economy_status['average_cash_balance'],
                    self.structure_credit_name,
                    economy_status['average_structure_credit_balance'],
                    self.total_job_losses
                )
    
    def run(self):
        """Run the full simulation"""
        logger.info("Starting simulation...")
        
        # Check LLM backend availability
        if not self.llm_client.check_connection():
            if self.llm_provider == 'ollama':
                logger.error("Cannot connect to Ollama. Please make sure Ollama is running.")
            else:
                logger.error("Cannot run the configured CLI LLM backend: %s", self.llm_target)
            return
        
        # Initialize agents
        self.initialize_agents()
        
        # Run simulation
        try:
            while self.step < self.duration:
                self.step_simulation()
        except KeyboardInterrupt:
            logger.info("Simulation interrupted by user")
        except Exception as e:
            logger.error(f"Error during simulation: {e}", exc_info=True)
        
        logger.info("Simulation completed")
    
    def get_statistics(self) -> Dict:
        """Get simulation statistics"""
        if not self.stats['place_occupancy']:
            return {}
        
        place_occupancy = np.array(self.stats['place_occupancy'])
        agents_in_place = np.array(self.stats['agents_in_place'])

        result = {
            'mean_occupancy': float(np.mean(place_occupancy)),
            'std_occupancy': float(np.std(place_occupancy)),
            'mean_agents_in_place': float(np.mean(agents_in_place)),
            'max_agents_in_place': int(np.max(agents_in_place)),
            'min_agents_in_place': int(np.min(agents_in_place)),
            'total_steps': self.step
        }

        if self.economy_enabled and self.stats['employed_agents']:
            employed_agents = np.array(self.stats['employed_agents'])
            result.update({
                'mean_employed_agents': float(np.mean(employed_agents)),
                'mean_unemployment_rate': float(
                    np.mean(np.array(self.stats['unemployed_agents'])) / self.num_agents
                ),
                'total_job_losses': int(self.total_job_losses),
                'final_average_cash_balance': float(self.stats['average_cash_balance'][-1]),
                'final_average_structure_credit_balance': float(
                    self.stats['average_structure_credit_balance'][-1]
                ),
            })

        return result
