import jax

from .base import (
    inverse_transform_dist,
    transform_tree,
    transform_dist_params,
    log_prob_dist,
    get_full_rank,
    get_low_rank,
    get_mean_field,
)

from .base import Posterior
from .utils import initialize_params

import tensorflow_probability.substrates.jax as tfp

tfd = tfp.distributions


class ADVI:
    def __init__(
        self, prior, bijector, get_log_likelihood, vi_type="mean_field", rank=None, ordered_posterior_bijectors=None
    ):
        """Automatic Differentiation Variational Inference
        A model class that implements the ADVI algorithm.

        Args:
            prior (dict): A dictionary of prior distributions.
            bijector (dict): A dictionary of bijectors. The keys should be the same as the keys in `prior`.
            get_log_likelihood (function): A function that returns log likelihood of data. The function should take two arguments: `likelihood_params` and `aux`.
                                          example:
                                            prior = {"p_of_head": tfd.Beta(0.5, 0.5)}
                                            seed = jax.random.PRNGKey(0)
                                            likelihood_params = sample_dist(prior, seed) # likelihood_params = {"p_of_head": 0.5}
                                            def coin_toss_log_likelihood(likelihood_params, aux, data):
                                                p_of_head = likelihood_params["p_of_head"]
                                                return tfd.Bernoulli(probs=p_of_head).log_prob(data)

            vi_type (str, optional): type of variational inference ("mean_field", "full_rank", "low_rank"). Defaults to "mean_field".
            rank (int, optional): Rank of posterior covariance matrix in case where `vi_type` is "low_rank". Defaults to None.
            ordered_posterior_bijectors (dict, optional): A dictionary of bijectors for posterior parameters. `ordered` here means ordered as per the alphabetical order. Defaults to None.
                                                  example:
                                                    In case of "mean_field" variational inference, the posterior is a `tfd.MultivariateNormalDiag`.
                                                    So, the ordered_posterior_bijectors can be [tfb.Identity(), tfb.Exp()] corresponding to `loc` and `scale_diag`.
        """

        assert bijector.keys() == prior.keys(), "The keys in `prior` and `bijector` must be the same."

        self.prior = prior
        self.check_prior_zero_batch()  # Assert that the prior distribution has no batch dimension.
        assert (vi_type == "low_rank") == (
            rank is not None
        ), "`rank` must be specified only if `vi_type` is `low_rank`."

        self.bijector = bijector
        self.get_log_likelihood = get_log_likelihood
        self.vi_type = vi_type

        self.approx_normal_prior = inverse_transform_dist(self.prior, self.bijector)

        if vi_type == "mean_field":
            self.posterior, self.unravel_fn, self.posterior_params_bijector = get_mean_field(
                self.approx_normal_prior, ordered_posterior_bijectors
            )
        elif vi_type == "low_rank":
            self.posterior, self.unravel_fn, self.posterior_params_bijector = get_low_rank(
                self.approx_normal_prior, rank, ordered_posterior_bijectors
            )
        elif vi_type == "full_rank":
            self.posterior, self.unravel_fn, self.posterior_params_bijector = get_full_rank(
                self.approx_normal_prior, ordered_posterior_bijectors
            )

    def check_prior_zero_batch(self):
        is_leaf = lambda x: isinstance(x, tfd.Distribution)
        batch_lens = jax.tree_map(lambda x: len(x.batch_shape), self.prior, is_leaf=is_leaf)
        assert sum(jax.tree_leaves(batch_lens)) == 0, "The prior distributions must have no batch dimension."

    def init(self, seed, initializer=jax.nn.initializers.normal()):
        return {"posterior": initialize_params(self.posterior, seed, initializer)}

    def loss_fn(self, params, batch, aux, data_size, seed, n_samples=1):
        posterior = params["posterior"]
        posterior = transform_dist_params(posterior, self.posterior_params_bijector)

        def loss_fn_per_sample(seed):
            sample = posterior.sample(seed=seed)
            q_log_prob = posterior.log_prob(sample)
            sample_tree = self.unravel_fn(sample)
            p_log_prob = log_prob_dist(self.approx_normal_prior, sample_tree)
            transformed_sample_tree = transform_tree(sample_tree, self.bijector)
            log_likelihood = self.get_log_likelihood(transformed_sample_tree, aux, batch, **params)
            log_likelihood = (log_likelihood / len(batch)) * data_size  # normalize by data size
            return (q_log_prob - p_log_prob - log_likelihood) / data_size

        seeds = jax.random.split(seed, n_samples)
        return jax.vmap(loss_fn_per_sample)(seeds).mean()

    def apply(self, params):
        posterior = params["posterior"]
        posterior = transform_dist_params(posterior, self.posterior_params_bijector)
        return Posterior(posterior, self.approx_normal_prior, self.bijector)
