"""
LLM-based agent in 2D worlds with multiple places.
"""
import argparse
import logging
import yaml
import os
import signal
import shutil
import time
import numpy as np
from typing import Optional, Tuple
from simulation import Simulation
from visualization import Visualizer

# Constants
DEFAULT_FRAME_INTERVAL_INTERACTIVE = 10
DEFAULT_FRAME_INTERVAL_CONFIG = 50
VISUALIZATION_UPDATE_DELAY = 0.2
LAST_SIGNAL = None


def setup_logging(config: dict):
    """Setup logging configuration"""
    log_config = config.get('logging', {})
    level = getattr(logging, log_config.get('level', 'INFO'))
    
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    handlers = [logging.StreamHandler()]
    
    if 'log_file' in log_config:
        handlers.append(logging.FileHandler(log_config['log_file']))
    
    logging.basicConfig(
        level=level,
        format=log_format,
        handlers=handlers
    )


def check_llm_setup(sim: Simulation, logger: logging.Logger) -> bool:
    """Check the configured LLM backend before running the simulation."""
    if not sim.llm_client.check_connection():
        if sim.llm_provider == 'ollama':
            logger.error("Cannot connect to Ollama. Please make sure Ollama is running.")
            logger.error(f"Expected URL: {sim.llm_client.base_url}")
        else:
            logger.error("Cannot run the configured CLI LLM backend.")
            logger.error(f"Configured command/model: {sim.llm_target}")
        return False

    if sim.llm_provider == 'ollama' and not sim.llm_client.check_model_exists():
        logger.warning(f"Model '{sim.llm_client.model}' not found in Ollama.")
        available_models = sim.llm_client.list_models()
        if available_models:
            logger.info(f"Available models: {', '.join(available_models)}")
            logger.info("Please update 'llm.model' in examples/spatial_demo/configs/config.yaml or download the model:")
            logger.info(f"  ollama pull {sim.llm_client.model}")
        else:
            logger.error("No models found in Ollama. Please download a model first.")
            logger.error(f"Example: ollama pull {sim.llm_client.model}")
        return False

    logger.info(
        "Using %s backend: %s",
        sim.llm_provider,
        sim.llm_target
    )
    return True


def determine_visualization_settings(args, config: dict) -> Tuple[bool, bool, int, str]:
    """Determine visualization settings from args and config"""
    config_save_frames = config.get('visualization', {}).get('save_frames', False)
    should_visualize = args.visualize or args.save_frames or config_save_frames
    
    frame_interval = (
        args.frame_interval or 
        config.get('visualization', {}).get('frame_interval', DEFAULT_FRAME_INTERVAL_CONFIG)
    )
    
    # For interactive visualization, use smaller interval
    if args.visualize and not args.save_frames and not args.frame_interval:
        frame_interval = DEFAULT_FRAME_INTERVAL_INTERACTIVE
    
    output_dir = config.get('visualization', {}).get('output_dir', 'outputs/spatial/output')
    
    return should_visualize, config_save_frames, frame_interval, output_dir


def handle_visualization(
    visualizer: Visualizer,
    sim: Simulation,
    step: int,
    frame_interval: int,
    should_save: bool,
    output_dir: str,
    logger: logging.Logger
):
    """Handle visualization for a simulation step"""
    if step % frame_interval != 0 and step != sim.duration - 1:
        return
    
    place_status = sim.get_place_status()
    economy_status = sim.get_economy_status() if sim.economy_enabled else None
    
    if should_save:
        save_path = os.path.join(output_dir, f"frame_{step:04d}.png")
        visualizer.visualize_step(
            sim.agents,
            place_status,
            step,
            communication_radius=sim.communication_radius,
            save_path=save_path,
            fire_states=sim.fire_states,
            economy_status=economy_status
        )
        logger.info(f"Saved frame: {save_path}")
    else:
        # Interactive visualization only
        logger.info(f"Displaying visualization for step {step}")
        try:
            visualizer.visualize_step(
                sim.agents,
                place_status,
                step,
                communication_radius=sim.communication_radius,
                fire_states=sim.fire_states,
                economy_status=economy_status
            )
            time.sleep(VISUALIZATION_UPDATE_DELAY)
        except Exception as e:
            logger.error(f"Error displaying visualization: {e}", exc_info=True)


def print_statistics(stats: dict, sim: Simulation, logger: logging.Logger):
    """Print simulation statistics"""
    logger.info("\n=== Simulation Statistics ===")
    logger.info(f"Total steps: {stats.get('total_steps', 0)}")
    logger.info(f"Overall mean occupancy: {stats.get('mean_occupancy', 0):.2%}")
    logger.info(f"Overall std occupancy: {stats.get('std_occupancy', 0):.2%}")
    logger.info(f"Mean agents in places: {stats.get('mean_agents_in_place', 0):.2f}")
    logger.info(f"Max agents in places: {stats.get('max_agents_in_place', 0)}")
    logger.info(f"Min agents in places: {stats.get('min_agents_in_place', 0)}")

    if 'mean_unemployment_rate' in stats:
        logger.info("\n=== Economy Statistics ===")
        logger.info(f"Mean employed agents: {stats.get('mean_employed_agents', 0):.2f}")
        logger.info(f"Mean unemployment rate: {stats.get('mean_unemployment_rate', 0):.2%}")
        logger.info(f"Total LLM-driven job losses: {stats.get('total_job_losses', 0)}")
        logger.info(
            f"Final average cash balance: {stats.get('final_average_cash_balance', 0):.2f}"
        )
        logger.info(
            "Final average structure-credit balance: "
            f"{stats.get('final_average_structure_credit_balance', 0):.2f}"
        )
    
    # Print per-place statistics
    if 'places' in sim.stats:
        logger.info("\n=== Per-Place Statistics ===")
        for place_name, place_stats in sim.stats['places'].items():
            if place_stats['occupancy']:
                occupancy_array = np.array(place_stats['occupancy'])
                agents_array = np.array(place_stats['agents_in_place'])
                logger.info(f"\n{place_name}:")
                logger.info(f"  Mean occupancy: {np.mean(occupancy_array):.2%}")
                logger.info(f"  Mean agents: {np.mean(agents_array):.2f}")
                logger.info(f"  Max agents: {int(np.max(agents_array))}")
                logger.info(f"  Min agents: {int(np.min(agents_array))}")


def main():
    """Main function"""
    parser = argparse.ArgumentParser(description='Simulation of LLM-based agent in 2D worlds with multiple places.')
    parser.add_argument(
        '--config',
        type=str,
        default='examples/spatial_demo/configs/config.yaml',
        help='Path to configuration file'
    )
    parser.add_argument(
        '--visualize',
        action='store_true',
        help='Enable visualization during simulation'
    )
    parser.add_argument(
        '--save-frames',
        action='store_true',
        help='Save visualization frames'
    )
    parser.add_argument(
        '--frame-interval',
        type=int,
        default=None,
        help='Interval between visualization frames (overrides config)'
    )
    
    args = parser.parse_args()
    
    # Load config
    with open(args.config, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # Setup logging
    setup_logging(config)
    logger = logging.getLogger(__name__)
    
    # Determine visualization settings
    should_visualize, config_save_frames, frame_interval, output_dir = \
        determine_visualization_settings(args, config)
    
    # Remove output directory if it exists
    if os.path.exists(output_dir):
        logger.info(f"Removing existing output directory: {output_dir}")
        shutil.rmtree(output_dir)
    
    # Create output directory if needed
    if args.save_frames or config_save_frames:
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Output directory: {output_dir}")
        for filename in ['messages.jsonl', 'memory_reasoning.jsonl']:
            open(os.path.join(output_dir, filename), 'a', encoding='utf-8').close()
        if config.get('economy', {}).get('enabled', False):
            open(os.path.join(output_dir, 'economy_events.jsonl'), 'a', encoding='utf-8').close()
    
    # Initialize simulation
    sim = Simulation(config_path=args.config, output_dir=output_dir)
    
    # Initialize visualizer if needed
    visualizer = None
    if should_visualize:
        visualizer = Visualizer(
            half_space_size=sim.half_space_size,
            places=sim.places,
            num_agents=sim.num_agents
        )

    def handle_signal(signum, frame):
        """Log the received signal with run context before unwinding."""
        del frame
        global LAST_SIGNAL
        LAST_SIGNAL = signum
        signal_name = signal.Signals(signum).name
        logger.warning(
            "Received %s at step %s (pid=%s, ppid=%s)",
            signal_name,
            sim.step,
            os.getpid(),
            os.getppid(),
        )
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sighup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, handle_signal)
    
    # Run simulation
    try:
        # Initialize agents
        sim.initialize_agents()
        
        # Check LLM backend setup
        if not check_llm_setup(sim, logger):
            return
        
        logger.info("Starting simulation...")
        
        # Run simulation steps
        while sim.step < sim.duration:
            sim.step_simulation()
            
            # Visualize if needed
            if visualizer and should_visualize:
                should_save = args.save_frames or (config_save_frames and not args.visualize)
                handle_visualization(
                    visualizer, sim, sim.step, frame_interval,
                    should_save, output_dir, logger
                )
        
        logger.info("Simulation completed")
        
        # Print statistics
        stats = sim.get_statistics()
        print_statistics(stats, sim, logger)
        
        # Plot statistics
        if visualizer:
            should_save_stats = args.save_frames or config_save_frames
            stats_path = os.path.join(output_dir, 'statistics.png') if should_save_stats else None
            visualizer.plot_statistics(sim.stats, save_path=stats_path, fire_states=sim.fire_states)
            if stats_path:
                logger.info(f"Saved statistics plot: {stats_path}")
        
    except KeyboardInterrupt:
        if LAST_SIGNAL is not None:
            logger.info(
                "Simulation interrupted by signal %s",
                signal.Signals(LAST_SIGNAL).name,
            )
        else:
            logger.info("Simulation interrupted by KeyboardInterrupt")
    except SystemExit as e:
        if LAST_SIGNAL is not None:
            logger.info(
                "Simulation terminated by signal %s",
                signal.Signals(LAST_SIGNAL).name,
            )
        else:
            raise e
    except Exception as e:
        logger.error(f"Error during simulation: {e}", exc_info=True)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        if hasattr(signal, "SIGHUP") and previous_sighup is not None:
            signal.signal(signal.SIGHUP, previous_sighup)


if __name__ == "__main__":
    main()
